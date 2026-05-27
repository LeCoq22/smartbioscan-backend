"""
SmartTanita — API FastAPI
Corre local en puerto 8000, luego se despliega en Railway/Render.

Arrancar:
    cd ~/Downloads/ProyectSmartTanita
    uvicorn api.main:app --reload --port 8000
"""

import asyncio
import logging
import os
import re
import secrets
import sys
import time as _time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Depends, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response as HTMLResponse
from jose import jwt as _jose_jwt, JWTError as _JWTError
from pydantic import BaseModel, EmailStr

_logger = logging.getLogger("smartbioscan.api")

load_dotenv()

# El pipeline vive un nivel arriba de api/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from api.payments import router as payments_router
from api.email import send_waitlist_confirmation, send_welcome_email, send_password_reset_email
from api.subscriptions import register_routes as _register_subscription_routes
from api.error_logging import BackendErrorLoggingMiddleware


# ─────────────────────────────────────────────
# APP
# ─────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("SmartTanita API arrancando...")
    try:
        await _get_jwks_cached()
        _logger.info("JWKS precargado: %d keys", len((_jwks_data or {}).get("keys", [])))
    except Exception as exc:
        _logger.warning("No se pudo precargar JWKS en startup: %s", exc)
    yield
    print("SmartTanita API apagándose...")

app = FastAPI(
    title="SmartTanita API",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(payments_router)

_frontend_url = os.getenv("FRONTEND_URL", "")
_landing_url  = os.getenv("LANDING_URL", "")
_allowed_origins = [o for o in [
    "http://localhost:5173",
    "http://localhost:5174",
    "http://localhost:3000",
    "https://www.smartbioscan.com",
    "https://smartbioscan.com",
    _frontend_url,
    _landing_url,
] if o]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Captura de errores 5xx en la tabla backend_errors (después de CORS para
# que los CORS headers se apliquen también a respuestas de error)
app.add_middleware(BackendErrorLoggingMiddleware)


# ─────────────────────────────────────────────
# JWT / JWKS — Verificación de tokens Supabase (ES256)
# ─────────────────────────────────────────────

_JWKS_URL        = os.getenv(
    "SUPABASE_JWKS_URL",
    "https://ivwderljwrwjzmgcmens.supabase.co/auth/v1/.well-known/jwks.json",
)
_JWKS_TTL        = 1800           # segundos — refresca cada 30 min
_jwks_data: Optional[dict] = None
_jwks_fetched_at: float    = 0.0


def _fetch_jwks_sync() -> dict:
    resp = httpx.get(_JWKS_URL, timeout=5.0)
    resp.raise_for_status()
    return resp.json()


async def _get_jwks_cached(force: bool = False) -> dict:
    global _jwks_data, _jwks_fetched_at
    now = _time.time()
    if not force and _jwks_data is not None and (now - _jwks_fetched_at) < _JWKS_TTL:
        return _jwks_data
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, _fetch_jwks_sync)
    _jwks_data       = data
    _jwks_fetched_at = _time.time()
    return data


async def _verify_supabase_jwt(token: str) -> dict:
    """Verifica JWT de Supabase (ES256/JWKS). Retorna payload o lanza HTTPException."""
    try:
        header = _jose_jwt.get_unverified_header(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Token malformado")

    kid = header.get("kid")
    if not kid:
        raise HTTPException(status_code=401, detail="Token malformado: sin kid")

    def _find_key(jwks: dict) -> Optional[dict]:
        return next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)

    try:
        jwks = await _get_jwks_cached()
    except Exception as exc:
        _logger.error("JWKS fetch error: %s", exc)
        raise HTTPException(status_code=503, detail="Servicio de autenticación no disponible")

    key_data = _find_key(jwks)
    if key_data is None:
        # kid desconocido — puede que rotaron la key; refrescar una vez
        try:
            jwks = await _get_jwks_cached(force=True)
        except Exception as exc:
            _logger.error("JWKS re-fetch error: %s", exc)
            raise HTTPException(status_code=503, detail="Servicio de autenticación no disponible")
        key_data = _find_key(jwks)

    if key_data is None:
        raise HTTPException(status_code=401, detail="Token inválido: clave desconocida")

    try:
        payload = _jose_jwt.decode(
            token, key_data,
            algorithms=["ES256"],
            options={"verify_aud": False},
        )
        return payload
    except _JWTError as exc:
        raise HTTPException(status_code=401, detail=f"Token inválido: {exc}")


# ─────────────────────────────────────────────
# AUTH — valida que el request viene del Nutri correcto
# ─────────────────────────────────────────────

async def get_current_nutri(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_nutri_id: Optional[str] = Header(None, alias="X-Nutri-Id"),
) -> str:
    """Verifica JWT de Supabase y retorna el nutri_id del payload (sub)."""
    ip = request.client.host if request.client else "unknown"

    if x_nutri_id:
        _logger.warning(
            "X-Nutri-Id obsoleto recibido en %s desde %s — ignorado",
            request.url.path, ip,
        )

    if not authorization or not authorization.startswith("Bearer "):
        _logger.warning(
            "auth_reject path=%s ip=%s reason=missing_bearer",
            request.url.path, ip,
        )
        raise HTTPException(status_code=401, detail="Authorization header requerido")

    token = authorization.removeprefix("Bearer ")
    payload = await _verify_supabase_jwt(token)

    nutri_id = payload.get("sub")
    if not nutri_id:
        raise HTTPException(status_code=401, detail="Token inválido: sin sub")

    try:
        from db import DB
        db = DB()
        res = db.client.table('nutris').select('id').eq('id', nutri_id).single().execute()
        if not res.data:
            _logger.warning(
                "auth_reject path=%s ip=%s sub=%s iat=%s exp=%s reason=nutri_not_found",
                request.url.path, ip, nutri_id, payload.get("iat"), payload.get("exp"),
            )
            raise HTTPException(status_code=401, detail="Usuario no registrado")
    except HTTPException:
        raise
    except Exception as exc:
        _logger.error("auth DB error path=%s sub=%s: %s", request.url.path, nutri_id, exc)
        raise HTTPException(status_code=401, detail="Error al verificar usuario")

    return nutri_id


# ─────────────────────────────────────────────
# SCHEMAS
# ─────────────────────────────────────────────

class GenerateReportRequest(BaseModel):
    patient_id: str
    measurement_date: Optional[str] = None  # YYYY-MM-DD; si None → usa la más reciente
    output_path: Optional[str] = None  # solo para debugging local

class GenerateReportResponse(BaseModel):
    ok: bool
    report_id: Optional[str] = None
    pdf_url: Optional[str] = None
    error: Optional[str] = None
    generation_secs: Optional[float] = None
    skipped: Optional[bool] = None
    message: Optional[str] = None

class CreatePatientRequest(BaseModel):
    full_name: str
    tanita_email: EmailStr
    tanita_password: str
    phone_whatsapp: Optional[str] = None
    mytanita_profile_id: Optional[str] = None

class CreatePatientResponse(BaseModel):
    ok: bool
    patient_id: Optional[str] = None
    error: Optional[str] = None

class ProfileInfo(BaseModel):
    profile_id: Optional[str] = None
    profile_name: str

class VerifyCredentialsRequest(BaseModel):
    tanita_email: EmailStr
    tanita_password: str

class VerifyCredentialsResponse(BaseModel):
    ok: bool
    profiles: Optional[list[ProfileInfo]] = None
    error: Optional[str] = None

class RegisterNutriRequest(BaseModel):
    full_name: str
    email: EmailStr
    password: str
    specialty: Optional[str] = None

class RegisterNutriResponse(BaseModel):
    ok: bool
    nutri_id: Optional[str] = None
    error: Optional[str] = None

# ── Onboarding schemas ────────────────────────
class WaitlistRequest(BaseModel):
    nombre: str
    email: str
    profesion: Optional[str] = None

class WaitlistResponse(BaseModel):
    success: bool
    message: str

class ValidateTokenResponse(BaseModel):
    valid: bool
    reason: Optional[str] = None      # not_found | already_used | expired
    email: Optional[str] = None
    full_name: Optional[str] = None

class SetPasswordRequest(BaseModel):
    token: str
    password: str

class SetPasswordResponse(BaseModel):
    ok: bool
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    user: Optional[dict] = None
    message: Optional[str] = None

class LoginHintRequest(BaseModel):
    email: str

class LoginHintResponse(BaseModel):
    hint: str   # not_registered | wrong_password


class ForgotPasswordRequest(BaseModel):
    email: str


class ForgotPasswordResponse(BaseModel):
    ok: bool
    # 'sent'      → email enviado
    # 'not_nutri' → email no figura como nutricionista
    # 'suspended' → cuenta suspendida (no enviamos)
    status: str
    message: str


class ApproveWaitlistResponse(BaseModel):
    ok: bool
    nutri_id: Optional[str] = None
    invite_id: Optional[str] = None
    expires_at: Optional[str] = None
    set_password_url: Optional[str] = None

class RejectWaitlistRequest(BaseModel):
    reason: Optional[str] = None

class RejectWaitlistResponse(BaseModel):
    ok: bool

class CreateAdminNutriRequest(BaseModel):
    email: EmailStr
    full_name: str
    profesion: Optional[str] = None
    phone: Optional[str] = None

class CreateAdminNutriResponse(BaseModel):
    ok: bool
    nutri_id: Optional[str] = None
    invite_id: Optional[str] = None
    expires_at: Optional[str] = None
    set_password_url: Optional[str] = None

class NutriMeResponse(BaseModel):
    id: str
    email: str
    full_name: str
    display_signature: Optional[str] = None
    subscription_status: Optional[str] = None
    subscription_type: Optional[str] = None
    subscription_end: Optional[str] = None
    subscription_next_billing_date: Optional[str] = None
    max_reports_month: Optional[int] = None
    max_patients: Optional[int] = None

class UpdateNutriMeRequest(BaseModel):
    display_signature: str


# ─────────────────────────────────────────────
# AUTH DEPENDENCIES
# ─────────────────────────────────────────────

async def get_admin_nutri(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_nutri_id: Optional[str] = Header(None, alias="X-Nutri-Id"),
) -> str:
    """Verifica JWT de Supabase y retorna nutri_id si el usuario tiene role=admin."""
    ip = request.client.host if request.client else "unknown"

    if x_nutri_id:
        _logger.warning(
            "X-Nutri-Id obsoleto recibido en %s desde %s — ignorado",
            request.url.path, ip,
        )

    if not authorization or not authorization.startswith("Bearer "):
        _logger.warning(
            "admin_auth_reject path=%s ip=%s reason=missing_bearer",
            request.url.path, ip,
        )
        raise HTTPException(status_code=401, detail="Authorization header requerido")

    token = authorization.removeprefix("Bearer ")
    payload = await _verify_supabase_jwt(token)

    nutri_id = payload.get("sub")
    if not nutri_id:
        raise HTTPException(status_code=401, detail="Token inválido: sin sub")

    try:
        from db import DB
        db = DB()
        res = db.client.table('nutris').select('role').eq('id', nutri_id).single().execute()
        if not res.data:
            _logger.warning(
                "admin_auth_reject path=%s ip=%s sub=%s iat=%s exp=%s reason=nutri_not_found",
                request.url.path, ip, nutri_id, payload.get("iat"), payload.get("exp"),
            )
            raise HTTPException(status_code=401, detail="Usuario no registrado")
        role = res.data.get('role')
        if role != 'admin':
            _logger.warning(
                "admin_auth_reject path=%s ip=%s sub=%s role=%s iat=%s exp=%s reason=not_admin",
                request.url.path, ip, nutri_id, role, payload.get("iat"), payload.get("exp"),
            )
            raise HTTPException(status_code=403, detail="Acceso de admin requerido")
    except HTTPException:
        raise
    except Exception as exc:
        _logger.error("admin_auth DB error path=%s sub=%s: %s", request.url.path, nutri_id, exc)
        raise HTTPException(status_code=403, detail="No se pudo verificar el rol admin")

    return nutri_id


# ─────────────────────────────────────────────
# Admin: regeneración de reportes (módulo separado)
# ─────────────────────────────────────────────
from api.admin_regenerate import register_routes as _register_admin_regen_routes
_register_admin_regen_routes(app, get_admin_nutri)

_register_subscription_routes(app, get_current_nutri)


# ─────────────────────────────────────────────
# RATE LIMITER (in-memory, sin dependencias extra)
# ─────────────────────────────────────────────

_rate_hits: dict[str, list[float]] = defaultdict(list)
_rate_lock = Lock()

def _allow_rate(ip: str, limit: int = 5, window: int = 60) -> bool:
    """Retorna True si el request está dentro del límite. 5/min por defecto."""
    now = _time.time()
    with _rate_lock:
        hits = _rate_hits[ip]
        hits[:] = [t for t in hits if now - t < window]
        if len(hits) >= limit:
            return False
        hits.append(now)
        return True


# ─────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "service": "SmartTanita API"}


@app.post("/reports/generate", response_model=GenerateReportResponse)
async def generate_report(
    body: GenerateReportRequest,
    nutri_id: str = Depends(get_current_nutri),
):
    """
    Genera el reporte PDF de un paciente.
    El pipeline hace: login → CSV → análisis → PDF → Supabase Storage.
    
    Request:
        POST /reports/generate
        X-Nutri-Id: <nutri_uuid>
        { "patient_id": "<patient_uuid>" }

    Response:
        { "ok": true, "report_id": "...", "pdf_url": "...", "generation_secs": 18.3 }
    """
    import time
    from pipeline_v2 import run_pipeline

    t0 = time.time()
    try:
        result = await run_pipeline(
            email            = '',     # pipeline los obtiene de Supabase via patient_id
            password         = '',
            patient_id       = body.patient_id,
            nutri_id         = nutri_id,
            output_path      = body.output_path,
            measurement_date = body.measurement_date,
            use_db           = True,
        )
    except HTTPException:
        raise
    except Exception as e:
        _logger.exception(
            "Error generando reporte | patient_id=%s | measurement_date=%s",
            body.patient_id, body.measurement_date,
        )
        raise HTTPException(status_code=500, detail=str(e))

    elapsed = round(time.time() - t0, 1)

    if result.get('skipped'):
        return GenerateReportResponse(
            ok=True,
            skipped=True,
            message="No hay mediciones nuevas desde el último reporte",
        )

    if not result.get('ok'):
        error = result.get('error', 'unknown')
        # Errores conocidos con códigos HTTP apropiados
        if error == 'login_failed':
            raise HTTPException(status_code=422, detail="Credenciales MyTanita inválidas")
        if error == 'no_credentials':
            raise HTTPException(status_code=404, detail="Paciente sin credenciales configuradas")
        if error.startswith('quota_'):
            raise HTTPException(status_code=429, detail=f"Quota de reportes agotada: {error}")
        raise HTTPException(status_code=500, detail=error)

    # Construir URL pública del PDF en Supabase Storage
    pdf_url = None
    if result.get('report_id'):
        supabase_url = os.getenv('SUPABASE_URL', '')
        pdf_url = f"{supabase_url}/storage/v1/object/public/reports/{nutri_id}/{result['report_id']}.pdf"

    return GenerateReportResponse(
        ok              = True,
        report_id       = result.get('report_id'),
        pdf_url         = pdf_url,
        generation_secs = elapsed,
    )


@app.get("/patients")
async def list_patients(nutri_id: str = Depends(get_current_nutri)):
    """
    Lista los pacientes del Nutri autenticado.
    """
    try:
        from db import DB
        db = DB()
        patients = db.get_patients(nutri_id)
        return {"ok": True, "patients": patients}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/patients", response_model=CreatePatientResponse)
async def create_patient(
    body: CreatePatientRequest,
    nutri_id: str = Depends(get_current_nutri),
):
    """
    Da de alta un nuevo paciente con sus credenciales MyTanita.
    Las credenciales se encriptan con Fernet antes de guardarse.
    """
    try:
        from db import DB
        db = DB()
        patient = db.create_patient(nutri_id, {
            'full_name':           body.full_name,
            'phone_whatsapp':      body.phone_whatsapp,
            'mytanita_profile_id': body.mytanita_profile_id,
        })
        db.upsert_tanita_credentials(patient['id'], body.tanita_email, body.tanita_password)
        return CreatePatientResponse(ok=True, patient_id=patient['id'])
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        err_str = str(e).lower()
        if 'unique' in err_str or 'duplicate' in err_str or '23505' in err_str:
            raise HTTPException(status_code=409, detail="Ya existe un paciente con estas credenciales.")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/reports/{report_id}/html")
async def get_report_html(
    report_id: str,
    nutri_id: str = Query(..., alias="nutri_id"),
):
    """
    Devuelve el HTML del reporte desde Supabase Storage.
    Acepta nutri_id como query param para poder abrirse en nueva pestaña sin headers.
    """
    from db import DB
    db = DB()

    res = (db.client.table('reports')
           .select('pdf_storage_path,nutri_id')
           .eq('id', report_id)
           .eq('nutri_id', nutri_id)
           .execute())

    if not res.data:
        raise HTTPException(status_code=404, detail="Reporte no encontrado")

    pdf_path = res.data[0].get('pdf_storage_path', '')
    html_path = pdf_path.replace('.pdf', '.html')

    try:
        html_bytes = db.client.storage.from_('reports').download(html_path)
        return HTMLResponse(content=html_bytes, media_type='text/html; charset=utf-8')
    except Exception:
        raise HTTPException(status_code=404, detail="HTML no disponible para este reporte")


@app.get("/reports/{report_id}/pdf")
async def download_report_pdf(
    report_id: str,
    nutri_id: str = Depends(get_current_nutri),
):
    """
    Descarga el PDF del reporte como archivo binario con Content-Disposition.
    El nombre del archivo incluye el nombre del paciente y la fecha.
    """
    import re
    from db import DB
    db = DB()

    res = (db.client.table('reports')
           .select('pdf_storage_path, measurement_date, patient_id')
           .eq('id', report_id)
           .eq('nutri_id', nutri_id)
           .execute())

    if not res.data:
        raise HTTPException(status_code=404, detail="Reporte no encontrado")

    row = res.data[0]
    pdf_path = row.get('pdf_storage_path', '')
    measurement_date = (row.get('measurement_date') or '')[:10]

    patient_res = (db.client.table('patients')
                   .select('full_name')
                   .eq('id', row['patient_id'])
                   .limit(1)
                   .execute())
    patient_name = patient_res.data[0].get('full_name', 'paciente') if patient_res.data else 'paciente'

    safe_name = re.sub(r'[^a-z0-9]+', '-', patient_name.lower()).strip('-')
    filename = f"reporte-{safe_name}-{measurement_date}.pdf"

    try:
        pdf_bytes = db.client.storage.from_('reports').download(pdf_path)
    except Exception:
        raise HTTPException(status_code=404, detail="PDF no disponible para este reporte")

    return HTMLResponse(
        content=pdf_bytes,
        media_type='application/pdf',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


@app.post("/patients/verify-credentials", response_model=VerifyCredentialsResponse)
async def verify_credentials(
    body: VerifyCredentialsRequest,
    nutri_id: str = Depends(get_current_nutri),
):
    """
    Verifica credenciales MyTanita y devuelve la lista de perfiles de la cuenta.
    """
    from tanita_scraper import verify_and_list_profiles
    result = await verify_and_list_profiles(body.tanita_email, body.tanita_password)
    return VerifyCredentialsResponse(
        ok=result["ok"],
        profiles=result.get("profiles"),
        error=result.get("error"),
    )


@app.post("/patients/{patient_id}/sync-csvs")
async def sync_patient_csvs(
    patient_id: str,
    nutri_id: str = Depends(get_current_nutri),
):
    """
    Scrapes MyTanita and upserts all measurements into patient_csvs.
    Triggered by the "Actualizar" button on the patient reports screen.
    """
    from db import DB
    from tanita_scraper import scrape_profile_csv, extract_all_measurements

    db = DB()
    patient = db.get_patient(patient_id)
    if not patient or patient['nutri_id'] != nutri_id:
        raise HTTPException(status_code=404, detail="Paciente no encontrado")

    creds = db.get_tanita_credentials(patient_id)
    if not creds:
        raise HTTPException(status_code=404, detail="Sin credenciales configuradas")

    result = await scrape_profile_csv(
        creds['tanita_email'],
        creds['tanita_password'],
        patient.get('mytanita_profile_id'),
    )

    if not result.get('success'):
        error = result.get('error', 'unknown')
        db.update_scrape_status(patient_id, 'login_failed' if 'login' in error else 'timeout', error)
        if error == 'login_failed':
            raise HTTPException(status_code=422, detail="Credenciales MyTanita inválidas")
        raise HTTPException(status_code=500, detail=error)

    meas_list = extract_all_measurements(result['dataframe'])
    count = db.upsert_patient_csvs(patient_id, nutri_id, meas_list)
    db.update_scrape_status(patient_id, 'ok')

    latest = result.get('latest') or {}
    return {
        "ok": True,
        "synced": count,
        "latest_date": latest.get('date'),
    }


@app.get("/patients/{patient_id}/csvs")
async def get_patient_csvs(
    patient_id: str,
    nutri_id: str = Depends(get_current_nutri),
    limit: int = Query(50, ge=1, le=200),
):
    """
    Devuelve las mediciones almacenadas en patient_csvs para un paciente.
    """
    from db import DB
    db = DB()
    patient = db.get_patient(patient_id)
    if not patient or patient['nutri_id'] != nutri_id:
        raise HTTPException(status_code=404, detail="Paciente no encontrado")

    rows = db.get_patient_csvs(patient_id, limit=limit)
    return {"ok": True, "measurements": rows}


@app.delete("/patients/{patient_id}")
async def delete_patient(
    patient_id: str,
    nutri_id: str = Depends(get_current_nutri),
):
    """
    Soft delete de un paciente: copia paciente + reportes a shadow tables
    y borra los registros originales (transacción atómica vía RPC Postgres).
    Requiere ejecutar migrations/001_shadow_tables.sql en Supabase primero.
    """
    try:
        from db import DB
        db = DB()
        result = db.soft_delete_patient(patient_id, nutri_id)
        return {"ok": True, **result}
    except Exception as e:
        err_str = str(e)
        if 'no encontrado' in err_str.lower() or 'not found' in err_str.lower():
            raise HTTPException(status_code=404, detail="Paciente no encontrado")
        raise HTTPException(status_code=500, detail=err_str)


@app.post("/auth/register", response_model=RegisterNutriResponse)
async def register_nutri(body: RegisterNutriRequest):
    """
    Registra un nuevo Nutricionista.
    Fase 2: esto se reemplaza por Supabase Auth + invite flow.
    Por ahora crea la fila en la tabla nutritionists directamente.
    """
    try:
        from db import DB
        db = DB()
        nutri = db.create_nutri(
            full_name = body.full_name,
            email     = body.email,
            password  = body.password,   # db.py hashea con bcrypt
            specialty = body.specialty,
        )
        return RegisterNutriResponse(ok=True, nutri_id=nutri['id'])
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# PERFIL PROPIO DEL NUTRI
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/nutris/me", response_model=NutriMeResponse)
async def get_nutri_me(nutri_id: str = Depends(get_current_nutri)):
    """Retorna el perfil del nutri autenticado."""
    from db import DB
    db = DB()
    res = (db.client.table('nutris')
           .select(
               'id, email, full_name, display_signature, '
               'subscription_status, subscription_type, subscription_end, '
               'subscription_next_billing_date, max_reports_month, max_patients'
           )
           .eq('id', nutri_id)
           .single()
           .execute())
    if not res.data:
        raise HTTPException(status_code=404, detail="Nutri no encontrado")
    return res.data


@app.patch("/api/nutris/me")
async def update_nutri_me(
    body: UpdateNutriMeRequest,
    nutri_id: str = Depends(get_current_nutri),
):
    """Actualiza el display_signature del nutri autenticado."""
    sig = body.display_signature.strip()
    if not sig:
        raise HTTPException(status_code=422, detail="La firma no puede estar vacía")
    if len(sig) > 120:
        raise HTTPException(status_code=422, detail="La firma no puede superar 120 caracteres")
    if any(c in sig for c in ('\n', '\r')):
        raise HTTPException(status_code=422, detail="La firma no puede contener saltos de línea")
    if any(ord(c) < 32 for c in sig):
        raise HTTPException(status_code=422, detail="La firma contiene caracteres no permitidos")

    from db import DB
    db = DB()
    db.client.table('nutris').update({'display_signature': sig}).eq('id', nutri_id).execute()
    return {"ok": True, "display_signature": sig}


# ═══════════════════════════════════════════════════════════════════════════════
# ONBOARDING — Waitlist + Invites
# ═══════════════════════════════════════════════════════════════════════════════

_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

_PROFESION_LABELS = {
    'nutricionista': 'Nutricionista',
    'medico':        'Médico clínico',
    'entrenador':    'Entrenador personal',
    'otro':          'Profesional de salud',
}

def _humanize_profesion(profesion: Optional[str]) -> str:
    return _PROFESION_LABELS.get(profesion or '', '')


def _create_nutri_and_invite(db, user_id: str, email: str, full_name: str,
                              origin: str, profesion: Optional[str]) -> dict:
    """
    Crea nutri + beta_invite. Llama después de crear auth user.
    Retorna {'invite_id': ..., 'token': ..., 'expires_at': ...}.
    """
    frontend_url = os.getenv('FRONTEND_URL', 'https://app.smartbioscan.com')
    notes = f"Profesión: {profesion}" if profesion else None
    prof_label = _humanize_profesion(profesion)
    display_signature = f"{full_name} - {prof_label}" if prof_label else full_name

    db.client.table('nutris').insert({
        'id':                 user_id,
        'email':              email,
        'full_name':          full_name,
        'origin':             origin,
        'subscription_type':  'beta',
        'subscription_status':'active',
        'max_reports_month':  50,
        'max_patients':       20,
        'role':               'user',
        'notes':              notes,
        'display_signature':  display_signature,
    }).execute()

    token      = secrets.token_hex(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    inv = db.client.table('beta_invites').insert({
        'nutri_id':   user_id,
        'token':      token,
        'expires_at': expires_at,
    }).execute()
    invite_id = inv.data[0]['id']

    set_pwd_url = f"{frontend_url}/set-password?token={token}"
    send_welcome_email(email, full_name, set_pwd_url)

    return {'invite_id': invite_id, 'token': token, 'expires_at': expires_at}


@app.post("/api/waitlist", response_model=WaitlistResponse)
async def join_waitlist(body: WaitlistRequest):
    """
    Recibe solicitud desde la landing. Guarda en waitlist y manda Email A.
    Si el email ya existe (cualquier status), devuelve 200 silencioso sin reenvío.
    """
    if not _EMAIL_RE.match(body.email or ''):
        raise HTTPException(status_code=422, detail="Email inválido")
    if not body.nombre or not body.nombre.strip():
        raise HTTPException(status_code=422, detail="El nombre es obligatorio")

    from db import DB
    db = DB()

    # ── Paso 1: insert — único lugar donde puede haber duplicate ─────────
    try:
        db.client.table('waitlist').insert({
            'nombre':    body.nombre.strip(),
            'email':     body.email.strip().lower(),
            'profesion': body.profesion,
        }).execute()
    except Exception as exc:
        err = str(exc).lower()
        if 'unique' in err or 'duplicate' in err or '23505' in err:
            return WaitlistResponse(success=True, message="Recibimos tu solicitud")
        raise HTTPException(status_code=500, detail="Error al procesar la solicitud")

    # ── Paso 2: Email A — nunca bloquea ni rompe el response ─────────────
    try:
        eid = send_waitlist_confirmation(body.email.strip().lower(), body.nombre.strip())
        if eid is None:
            _logger.error(
                "send_waitlist_confirmation retornó None para %s — revisar logs de email.py",
                body.email.strip().lower(),
            )
    except Exception as exc:
        _logger.error("Excepción enviando Email A a %s: %s", body.email.strip().lower(), exc)

    return WaitlistResponse(success=True, message="Recibimos tu solicitud")


@app.get("/api/auth/validate-token", response_model=ValidateTokenResponse)
async def validate_token(token: str = Query(...)):
    """Verifica si un token de invite es válido. Retorna email y nombre si OK."""
    from db import DB
    db = DB()

    inv = db.client.table('beta_invites').select('*').eq('token', token).execute()
    if not inv.data:
        raise HTTPException(status_code=404,
                            detail={"valid": False, "reason": "not_found"})

    row = inv.data[0]
    if row.get('used_at'):
        raise HTTPException(status_code=410,
                            detail={"valid": False, "reason": "already_used"})

    expires = datetime.fromisoformat(row['expires_at'].replace('Z', '+00:00'))
    if datetime.now(timezone.utc) > expires:
        raise HTTPException(status_code=410,
                            detail={"valid": False, "reason": "expired"})

    nutri = db.client.table('nutris').select('email, full_name') \
               .eq('id', row['nutri_id']).single().execute()

    return ValidateTokenResponse(
        valid=True,
        email=nutri.data['email'],
        full_name=nutri.data['full_name'],
    )


@app.post("/api/auth/set-password", response_model=SetPasswordResponse)
async def set_password(body: SetPasswordRequest):
    """
    Valida token + contraseña, actualiza password en auth, genera sesión.
    """
    # Validar contraseña en backend
    pwd = body.password
    if len(pwd) < 8:
        raise HTTPException(status_code=422, detail="La contraseña debe tener al menos 8 caracteres")
    if not re.search(r'[A-Z]', pwd):
        raise HTTPException(status_code=422, detail="La contraseña debe tener al menos 1 mayúscula")
    if not re.search(r'[0-9]', pwd):
        raise HTTPException(status_code=422, detail="La contraseña debe tener al menos 1 número")

    from db import DB
    db = DB()

    inv = db.client.table('beta_invites').select('*').eq('token', body.token).execute()
    if not inv.data:
        raise HTTPException(status_code=404, detail="Token no válido")
    row = inv.data[0]
    if row.get('used_at'):
        raise HTTPException(status_code=410, detail="Este link ya fue usado")
    expires = datetime.fromisoformat(row['expires_at'].replace('Z', '+00:00'))
    if datetime.now(timezone.utc) > expires:
        raise HTTPException(status_code=410, detail="Este link expiró")

    nutri_id = row['nutri_id']
    nutri = db.client.table('nutris').select('email, full_name') \
               .eq('id', nutri_id).single().execute()
    if not nutri.data:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")

    email     = nutri.data['email']
    full_name = nutri.data['full_name']

    # Actualizar contraseña via Admin API
    db.client.auth.admin.update_user_by_id(nutri_id, {"password": pwd})

    # Marcar invite como usado
    db.client.table('beta_invites') \
       .update({'used_at': datetime.now(timezone.utc).isoformat()}) \
       .eq('token', body.token).execute()

    # Generar sesión
    try:
        session = db.client.auth.sign_in_with_password({"email": email, "password": pwd})
        return SetPasswordResponse(
            ok=True,
            access_token=session.session.access_token,
            refresh_token=session.session.refresh_token,
            user={"id": nutri_id, "email": email, "full_name": full_name},
        )
    except Exception as exc:
        _logger.error("set_password: sign_in falló tras update_user: %s", exc)
        return SetPasswordResponse(
            ok=True,
            message="Contraseña establecida. Iniciá sesión desde la pantalla de login.",
        )


@app.post("/api/auth/login-hint", response_model=LoginHintResponse)
async def login_hint(request: Request, body: LoginHintRequest):
    """
    Llamado después de un intento de login fallido.
    Retorna 'not_registered' si el email no existe en nutris, 'wrong_password' si sí existe.
    Rate-limited: 5 requests/min por IP.
    """
    ip = request.client.host if request.client else "unknown"
    _logger.info("login-hint from %s domain=%s", ip, (body.email or '').split('@')[-1])

    if not _allow_rate(ip, limit=5, window=60):
        _logger.warning("login-hint rate-limited: %s", ip)
        raise HTTPException(status_code=429, detail="Demasiadas solicitudes. Intentá en un minuto.")

    from db import DB
    db = DB()
    res = db.client.table('nutris').select('id').eq('email', (body.email or '').lower()).execute()
    hint = 'wrong_password' if res.data else 'not_registered'
    return LoginHintResponse(hint=hint)


@app.post("/api/auth/forgot-password", response_model=ForgotPasswordResponse)
async def forgot_password(request: Request, body: ForgotPasswordRequest):
    """
    Endpoint público de reset de contraseña self-service.

    Reusa el flow existente de beta_invites + Email C + /set-password.

    Rate-limited: 3 requests/min por IP (más estricto que login-hint
    porque dispara envío de email real).

    Devuelve un status explícito ('not_nutri') si el email no está
    en nutris — decisión consciente para una beta cerrada B2B donde
    los usuarios suelen confundir SmartBioScan con MyTanita.
    """
    ip = request.client.host if request.client else "unknown"
    email_norm = (body.email or "").strip().lower()
    _logger.info(
        "forgot-password from %s email_domain=%s",
        ip,
        email_norm.split('@')[-1] if '@' in email_norm else 'n/a',
    )

    if not _allow_rate(ip, limit=3, window=60):
        _logger.warning("forgot-password rate-limited: %s", ip)
        raise HTTPException(status_code=429, detail="Demasiadas solicitudes. Intentá en un minuto.")

    if not email_norm or '@' not in email_norm:
        raise HTTPException(status_code=400, detail="Email inválido.")

    from db import DB
    db = DB()

    nutri = db.client.table('nutris') \
        .select('id, email, full_name, is_suspended') \
        .eq('email', email_norm) \
        .execute()

    if not nutri.data:
        return ForgotPasswordResponse(
            ok=True,
            status='not_nutri',
            message='Este email no figura como nutricionista en SmartBioScan. Si sos paciente, el sistema que usás es app.mytanita.eu. Si sos nutri, revisá con qué email te registraste.'
        )

    row = nutri.data[0]
    if row.get('is_suspended'):
        _logger.warning("forgot-password para nutri suspendido: %s", email_norm)
        return ForgotPasswordResponse(
            ok=True,
            status='suspended',
            message='Tu cuenta está suspendida. Contactanos a equipo@mail.smartbioscan.com'
        )

    nutri_id = row['id']
    full_name = row.get('full_name') or ''

    token = secrets.token_hex(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()

    db.client.table('beta_invites').insert({
        'nutri_id': nutri_id,
        'token': token,
        'expires_at': expires_at,
    }).execute()

    frontend_url = os.getenv('FRONTEND_URL', 'https://app.smartbioscan.com')
    set_pwd_url = f"{frontend_url}/set-password?token={token}"

    email_id = send_password_reset_email(email_norm, full_name, set_pwd_url)

    _logger.info(
        "forgot-password OK nutri_id=%s email_sent=%s",
        nutri_id, bool(email_id),
    )

    return ForgotPasswordResponse(
        ok=True,
        status='sent',
        message=f'Te enviamos un email a {email_norm} con instrucciones para restablecer tu contraseña.'
    )


@app.get("/admin/waitlist")
async def list_waitlist(
    status: str = "pending",
    admin_id: str = Depends(get_admin_nutri),
):
    """Lista entradas de la waitlist filtradas por status (default: pending)."""
    from db import DB
    db = DB()
    res = db.client.table('waitlist').select('*').eq('status', status).order('created_at').execute()
    return {"items": res.data, "total": len(res.data)}


@app.post("/admin/waitlist/{waitlist_id}/approve", response_model=ApproveWaitlistResponse)
async def approve_waitlist(
    waitlist_id: str,
    admin_id: str = Depends(get_admin_nutri),
):
    """
    Aprueba una solicitud de la waitlist:
    crea auth user + nutri + invite, manda Email B, actualiza waitlist.
    Si algo falla después de crear el auth user, hace rollback (delete_user).
    """
    from db import DB
    db = DB()

    w = db.client.table('waitlist').select('*').eq('id', waitlist_id).execute()
    if not w.data:
        raise HTTPException(status_code=404, detail="Solicitud no encontrada")
    wl = w.data[0]
    if wl['status'] != 'pending':
        raise HTTPException(status_code=400, detail=f"Solicitud ya procesada: {wl['status']}")

    email    = wl['email']
    nombre   = wl['nombre']
    profesion = wl.get('profesion')
    user_id  = None

    try:
        # 1. Crear auth user (email ya confirmado, password temporal descartado)
        auth_resp = db.client.auth.admin.create_user({
            "email":         email,
            "password":      secrets.token_hex(24),
            "email_confirm": True,
        })
        user_id = auth_resp.user.id

        # 2. Crear nutri + invite + mandar email
        result = _create_nutri_and_invite(db, user_id, email, nombre, 'waitlist', profesion)

        # 3. Marcar waitlist como aprobada
        db.client.table('waitlist').update({
            'status':      'approved',
            'approved_at': datetime.now(timezone.utc).isoformat(),
            'approved_by': admin_id,
        }).eq('id', waitlist_id).execute()

        frontend_url = os.getenv('FRONTEND_URL', 'https://app.smartbioscan.com')
        return ApproveWaitlistResponse(
            ok=True,
            nutri_id=user_id,
            invite_id=result['invite_id'],
            expires_at=result['expires_at'],
            set_password_url=f"{frontend_url}/set-password?token={result['token']}",
        )

    except HTTPException:
        raise
    except Exception as exc:
        if user_id:
            try:
                db.client.auth.admin.delete_user(user_id)
            except Exception:
                pass
        raise HTTPException(status_code=500, detail=f"Error al aprobar: {exc}")


@app.post("/admin/waitlist/{waitlist_id}/reject", response_model=RejectWaitlistResponse)
async def reject_waitlist(
    waitlist_id: str,
    body: RejectWaitlistRequest,
    admin_id: str = Depends(get_admin_nutri),
):
    """Rechaza una solicitud de la waitlist. Preparado para envío de email posterior."""
    from db import DB
    db = DB()

    w = db.client.table("waitlist").select("*").eq("id", waitlist_id).execute()
    if not w.data:
        raise HTTPException(status_code=404, detail="Solicitud no encontrada")
    wl = w.data[0]
    if wl["status"] != "pending":
        raise HTTPException(status_code=400, detail=f"Solicitud ya procesada: {wl['status']}")

    try:
        db.client.table("waitlist").update({
            "status":          "rejected",
            "rejected_at":     datetime.now(timezone.utc).isoformat(),
            "rejected_reason": body.reason,
        }).eq("id", waitlist_id).execute()
        return RejectWaitlistResponse(ok=True)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error al rechazar: {exc}")


@app.post("/admin/nutris", response_model=CreateAdminNutriResponse)
async def create_admin_nutri(
    body: CreateAdminNutriRequest,
    admin_id: str = Depends(get_admin_nutri),
):
    """
    Alta manual de un Nutri (Camino B).
    Crea auth user + nutri + invite, manda Email B.
    """
    from db import DB
    db = DB()

    existing = db.client.table('nutris').select('id').eq('email', body.email.lower()).execute()
    if existing.data:
        raise HTTPException(status_code=409, detail="Ya existe un Nutri con ese email")

    user_id = None
    try:
        auth_resp = db.client.auth.admin.create_user({
            "email":         body.email.lower(),
            "password":      secrets.token_hex(24),
            "email_confirm": True,
        })
        user_id = auth_resp.user.id

        result = _create_nutri_and_invite(
            db, user_id, body.email.lower(), body.full_name, 'manual', body.profesion
        )

        if body.phone:
            db.client.table('nutris').update({'phone': body.phone}).eq('id', user_id).execute()

        frontend_url = os.getenv('FRONTEND_URL', 'https://app.smartbioscan.com')
        return CreateAdminNutriResponse(
            ok=True,
            nutri_id=user_id,
            invite_id=result['invite_id'],
            expires_at=result['expires_at'],
            set_password_url=f"{frontend_url}/set-password?token={result['token']}",
        )

    except HTTPException:
        raise
    except Exception as exc:
        if user_id:
            try:
                db.client.auth.admin.delete_user(user_id)
            except Exception:
                pass
        raise HTTPException(status_code=500, detail=f"Error al crear Nutri: {exc}")


# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN — Reset de contraseña de un Nutri
# ═══════════════════════════════════════════════════════════════════════════════

class SendPasswordResetResponse(BaseModel):
    ok: bool
    invite_id: str
    expires_at: str
    email_sent: bool


@app.post("/admin/nutris/{nutri_id}/send-password-reset",
          response_model=SendPasswordResetResponse)
async def admin_send_password_reset(
    nutri_id: str,
    admin_id: str = Depends(get_admin_nutri),
):
    """
    Genera un token de reset y manda Email C (reset de contraseña) al Nutri.
    Reusa el flow /set-password existente (mismo endpoint que invite/welcome).
    NO toca la contraseña actual hasta que el Nutri abre el link y la cambia.
    """
    from db import DB
    db = DB()

    nutri = db.client.table('nutris') \
        .select('id, email, full_name, is_suspended') \
        .eq('id', nutri_id) \
        .single() \
        .execute()
    if not nutri.data:
        raise HTTPException(status_code=404, detail="Nutri no encontrado")
    if nutri.data.get('is_suspended'):
        raise HTTPException(status_code=409, detail="No se puede resetear contraseña de un Nutri suspendido")

    email = (nutri.data.get('email') or '').strip()
    full_name = nutri.data.get('full_name') or ''
    if not email:
        raise HTTPException(status_code=500, detail="Nutri sin email registrado")

    # Generar nuevo token (no invalidamos invites previos — el más nuevo se usa,
    # y los viejos se pueden marcar como expirados si no se usaron antes).
    token = secrets.token_hex(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()

    inv = db.client.table('beta_invites').insert({
        'nutri_id':   nutri_id,
        'token':      token,
        'expires_at': expires_at,
    }).execute()
    invite_id = inv.data[0]['id']

    frontend_url = os.getenv('FRONTEND_URL', 'https://app.smartbioscan.com')
    set_pwd_url = f"{frontend_url}/set-password?token={token}"

    email_id = send_password_reset_email(email, full_name, set_pwd_url)

    _logger.info(
        "admin_send_password_reset admin=%s target_nutri=%s email_sent=%s invite_id=%s",
        admin_id, nutri_id, bool(email_id), invite_id,
    )

    return SendPasswordResetResponse(
        ok=True,
        invite_id=invite_id,
        expires_at=expires_at,
        email_sent=bool(email_id),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN — Descargar PDF de cualquier reporte (sin chequeo de propiedad)
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/admin/reports/{report_id}/pdf")
async def admin_download_report_pdf(
    report_id: str,
    admin_id: str = Depends(get_admin_nutri),
):
    """
    Versión admin de download_report_pdf: descarga el PDF de cualquier reporte,
    independientemente del nutri_id propietario. Útil para validaciones internas
    (revisar reportes de otros nutris sin tener sus credenciales).
    """
    import re
    from db import DB
    db = DB()

    res = (db.client.table('reports')
           .select('pdf_storage_path, measurement_date, patient_id, nutri_id')
           .eq('id', report_id)
           .execute())

    if not res.data:
        raise HTTPException(status_code=404, detail="Reporte no encontrado")

    row = res.data[0]
    pdf_path = row.get('pdf_storage_path', '')
    measurement_date = (row.get('measurement_date') or '')[:10]

    patient_res = (db.client.table('patients')
                   .select('full_name')
                   .eq('id', row['patient_id'])
                   .limit(1)
                   .execute())
    patient_name = patient_res.data[0].get('full_name', 'paciente') if patient_res.data else 'paciente'

    safe_name = re.sub(r'[^a-z0-9]+', '-', patient_name.lower()).strip('-')
    filename = f"reporte-{safe_name}-{measurement_date}.pdf"

    try:
        pdf_bytes = db.client.storage.from_('reports').download(pdf_path)
    except Exception:
        raise HTTPException(status_code=404, detail="PDF no disponible para este reporte")

    _logger.info(
        "admin_download_report_pdf admin=%s report=%s nutri=%s patient=%s",
        admin_id, report_id, row.get('nutri_id'), patient_name,
    )

    return HTMLResponse(
        content=pdf_bytes,
        media_type='application/pdf',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


# ═══════════════════════════════════════════════════════════════════════════════
# FRONTEND ERROR LOGGING
# ═══════════════════════════════════════════════════════════════════════════════
# Endpoint público para que el frontend reporte excepciones no manejadas
# (window.onerror / unhandledrejection / errores manuales). Sin auth para
# capturar también errores pre-login. Rate-limited y truncado para evitar abuso.

class FrontendErrorRequest(BaseModel):
    message: str
    stack: Optional[str] = None
    url: Optional[str] = None
    user_agent: Optional[str] = None
    source: Optional[str] = None        # 'onerror' | 'unhandledrejection' | 'manual'
    nutri_id: Optional[str] = None      # Si está logueado, lo manda el frontend
    context: Optional[dict] = None      # JSON libre opcional


# Rate-limit muy simple en memoria: max 30 reportes por minuto por IP
_FE_ERROR_BUCKET: dict[str, list[float]] = defaultdict(list)
_FE_ERROR_BUCKET_LOCK = Lock()


def _fe_error_rate_limited(ip: str) -> bool:
    now = _time.time()
    window = 60.0
    limit = 30
    with _FE_ERROR_BUCKET_LOCK:
        bucket = _FE_ERROR_BUCKET[ip]
        # Limpieza
        cutoff = now - window
        bucket[:] = [t for t in bucket if t > cutoff]
        if len(bucket) >= limit:
            return True
        bucket.append(now)
        return False


@app.get("/admin/backend-errors")
async def list_backend_errors(
    limit: int = 100,
    path_contains: Optional[str] = None,
    admin_id: str = Depends(get_admin_nutri),
):
    """
    Lista los últimos errores 5xx capturados por BackendErrorLoggingMiddleware.
    Solo admin. Aplana el join con nutris para que el frontend reciba
    nutri_full_name y nutri_email como campos flat.
    """
    from db import DB
    db = DB()
    limit = max(1, min(int(limit or 100), 500))
    q = db.client.table('backend_errors').select(
        '*, nutris(full_name, email)'
    ).order('created_at', desc=True).limit(limit)
    if path_contains:
        q = q.ilike('request_path', f'%{path_contains}%')
    res = q.execute()
    rows = []
    for row in (res.data or []):
        nutri = row.pop('nutris', None) or {}
        row['nutri_full_name'] = nutri.get('full_name')
        row['nutri_email']     = nutri.get('email')
        rows.append(row)
    return {"errors": rows, "total": len(rows)}


@app.post("/errors/frontend")
async def log_frontend_error(body: FrontendErrorRequest, request: Request):
    """
    Recibe un error JS del frontend y lo guarda en `frontend_errors`.
    Endpoint público (sin auth) porque queremos capturar errores pre-login.
    Rate-limited a 30/min por IP.
    """
    ip = request.client.host if request.client else "unknown"
    if _fe_error_rate_limited(ip):
        return {"ok": False, "reason": "rate_limited"}

    # Truncar campos largos para evitar abuso
    def _trim(s: Optional[str], maxlen: int) -> Optional[str]:
        if s is None:
            return None
        return s if len(s) <= maxlen else s[:maxlen] + "...(truncated)"

    row = {
        "message":    _trim(body.message, 2000),
        "stack":      _trim(body.stack, 10000),
        "url":        _trim(body.url, 500),
        "user_agent": _trim(body.user_agent, 500),
        "source":     _trim(body.source, 50),
        "nutri_id":   body.nutri_id if body.nutri_id else None,
        "context":    body.context,
    }

    try:
        from db import DB
        db = DB()
        db.client.table("frontend_errors").insert(row).execute()
        _logger.warning(
            "frontend_error reported: nutri=%s source=%s message=%s",
            row.get("nutri_id") or "anonymous",
            row.get("source"),
            (row.get("message") or "")[:200],
        )
        return {"ok": True}
    except Exception as e:
        _logger.error("Failed to log frontend error: %s", e)
        # No le devolvemos 500 al frontend para evitar loops de errores
        return {"ok": False, "reason": "db_error"}

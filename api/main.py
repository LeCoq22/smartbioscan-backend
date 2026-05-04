"""
SmartTanita — API FastAPI
Corre local en puerto 8000, luego se despliega en Railway/Render.

Arrancar:
    cd ~/Downloads/ProyectSmartTanita
    uvicorn api.main:app --reload --port 8000
"""

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

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Depends, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response as HTMLResponse
from pydantic import BaseModel, EmailStr

_logger = logging.getLogger("smartbioscan.api")

load_dotenv()

# El pipeline vive un nivel arriba de api/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from api.payments import router as payments_router
from api.email import send_waitlist_confirmation, send_welcome_email


# ─────────────────────────────────────────────
# APP
# ─────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("SmartTanita API arrancando...")
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


# ─────────────────────────────────────────────
# AUTH — valida que el request viene del Nutri correcto
# ─────────────────────────────────────────────

async def get_current_nutri(x_nutri_id: str = Header(..., alias="X-Nutri-Id")):
    """
    Header simple por ahora. En Fase 2 esto pasa a JWT de Supabase Auth.
    El frontend manda: X-Nutri-Id: <uuid>
    """
    if not x_nutri_id:
        raise HTTPException(status_code=401, detail="X-Nutri-Id header requerido")
    return x_nutri_id


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

class ApproveWaitlistResponse(BaseModel):
    ok: bool
    nutri_id: Optional[str] = None
    invite_id: Optional[str] = None
    expires_at: Optional[str] = None
    set_password_url: Optional[str] = None

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


# ─────────────────────────────────────────────
# AUTH DEPENDENCIES
# ─────────────────────────────────────────────

async def get_admin_nutri(x_nutri_id: str = Header(..., alias="X-Nutri-Id")):
    """Verifica que el header X-Nutri-Id corresponde a un Nutri con role='admin'."""
    if not x_nutri_id:
        raise HTTPException(status_code=401, detail="X-Nutri-Id header requerido")
    try:
        from db import DB
        db = DB()
        res = db.client.table('nutris').select('role').eq('id', x_nutri_id).single().execute()
        if not res.data or res.data.get('role') != 'admin':
            raise HTTPException(status_code=403, detail="Acceso de admin requerido")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=403, detail="No se pudo verificar el rol admin")
    return x_nutri_id


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
    except Exception as e:
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
# ONBOARDING — Waitlist + Invites
# ═══════════════════════════════════════════════════════════════════════════════

_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


def _create_nutri_and_invite(db, user_id: str, email: str, full_name: str,
                              origin: str, profesion: Optional[str]) -> dict:
    """
    Crea nutri + beta_invite. Llama después de crear auth user.
    Retorna {'invite_id': ..., 'token': ..., 'expires_at': ...}.
    """
    frontend_url = os.getenv('FRONTEND_URL', 'https://app.smartbioscan.com')
    notes = f"Profesión: {profesion}" if profesion else None

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
    try:
        db.client.table('waitlist').insert({
            'nombre':    body.nombre.strip(),
            'email':     body.email.strip().lower(),
            'profesion': body.profesion,
        }).execute()
        send_waitlist_confirmation(body.email.strip().lower(), body.nombre.strip())
    except Exception as exc:
        err = str(exc).lower()
        if 'unique' in err or 'duplicate' in err or '23505' in err:
            # Email duplicado — silencioso
            return WaitlistResponse(success=True, message="Recibimos tu solicitud")
        raise HTTPException(status_code=500, detail="Error al procesar la solicitud")

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

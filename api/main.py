"""
SmartTanita — API FastAPI
Corre local en puerto 8000, luego se despliega en Railway/Render.

Arrancar:
    cd ~/Downloads/ProyectSmartTanita
    uvicorn api.main:app --reload --port 8000
"""

import os, sys
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Depends, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response as HTMLResponse
from pydantic import BaseModel, EmailStr
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

# El pipeline vive un nivel arriba de api/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:5174"],  # Vite dev
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

class CreatePatientResponse(BaseModel):
    ok: bool
    patient_id: Optional[str] = None
    error: Optional[str] = None

class VerifyCredentialsRequest(BaseModel):
    tanita_email: EmailStr
    tanita_password: str

class VerifyCredentialsResponse(BaseModel):
    ok: bool
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
            email       = '',     # pipeline los obtiene de Supabase via patient_id
            password    = '',
            patient_id  = body.patient_id,
            nutri_id    = nutri_id,
            output_path = body.output_path,
            use_db      = True,
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
            'full_name':      body.full_name,
            'phone_whatsapp': body.phone_whatsapp,
        })
        db.upsert_tanita_credentials(patient['id'], body.tanita_email, body.tanita_password)
        return CreatePatientResponse(ok=True, patient_id=patient['id'])
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
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


@app.post("/patients/verify-credentials", response_model=VerifyCredentialsResponse)
async def verify_credentials(
    body: VerifyCredentialsRequest,
    nutri_id: str = Depends(get_current_nutri),
):
    """
    Verifica credenciales MyTanita haciendo login real sin scraping.
    """
    from tanita_scraper import verify_login
    result = await verify_login(body.tanita_email, body.tanita_password)
    return VerifyCredentialsResponse(ok=result["ok"], error=result.get("error"))


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

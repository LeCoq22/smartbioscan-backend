"""
SmartTanita — Endpoints admin para regenerar reportes existentes.

Se usan para aplicar fixes del pdf_generator a reportes ya generados
SIN volver a scrapear MyTanita: leen el csv_raw guardado en la tabla
reports y re-ejecutan análisis + HTML + PDF, sobrescribiendo el PDF
en Storage al mismo path ({nutri_id}/{report_id}.pdf).

Por diseño NO insertan filas nuevas en reports (no hay PK colisión).
El measurement_date y demás metadatos no cambian.

Endpoints:
  POST /admin/reports/{report_id}/regenerate?dry_run=true|false
  POST /admin/reports/regenerate-batch
       body: { "dry_run": bool, "limit": int|null, "nutri_id": str|null }
"""

import io
import csv
import logging
import sys
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

# El pipeline vive un nivel arriba de api/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from analysis_engine import PatientInfo, TanitaMeasurement, analyze
from csv_parser import parse_float, parse_int
from pdf_generator_v2 import generate_html_v2 as generate_html

_logger = logging.getLogger("smartbioscan.admin_regenerate")

router = APIRouter(prefix="/admin/reports", tags=["admin-regenerate"])


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _parse_csv_string(csv_text: str) -> list:
    """
    Igual que csv_parser.load_csv pero desde string (csv_raw guardado).
    Devuelve lista de TanitaMeasurement ordenada por fecha.
    """
    if not csv_text or not csv_text.strip():
        raise ValueError("csv_raw vacío")

    measurements = []
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        m = TanitaMeasurement(
            date=row['Date'].strip().strip('"'),
            weight_kg=parse_float(row['Weight (kg)']),
            bmi=parse_float(row['BMI']),
            body_fat_pct=parse_float(row['Body Fat (%)']),
            visceral_fat=parse_float(row['Visc Fat']),
            muscle_mass_kg=parse_float(row['Muscle Mass (kg)']),
            muscle_quality=parse_float(row['Muscle Quality']),
            bone_mass_kg=parse_float(row['Bone Mass (kg)']),
            bmr_kcal=parse_float(row['BMR (kcal)']),
            metabolic_age=parse_int(row['Metab Age']),
            body_water_pct=parse_float(row['Body Water (%)']),
            physique_rating=parse_int(row['Physique Rating']),
            muscle_right_arm=parse_float(row['Muscle mass - right arm']),
            muscle_left_arm=parse_float(row['Muscle mass - left arm']),
            muscle_right_leg=parse_float(row['Muscle mass - right leg']),
            muscle_left_leg=parse_float(row['Muscle mass - left leg']),
            muscle_trunk=parse_float(row['Muscle mass - trunk']),
            quality_right_arm=parse_float(row['Muscle quality - right arm']),
            quality_left_arm=parse_float(row['Muscle quality - left arm']),
            quality_right_leg=parse_float(row['Muscle quality - right leg']),
            quality_left_leg=parse_float(row['Muscle quality - left leg']),
            quality_trunk=parse_float(row['Muscle quality - trunk']),
            fat_pct_right_arm=parse_float(row['Body fat (%) - right arm']),
            fat_pct_left_arm=parse_float(row['Body fat (%) - left arm']),
            fat_pct_right_leg=parse_float(row['Body fat (%) - right leg']),
            fat_pct_left_leg=parse_float(row['Body fat (%) - left leg']),
            fat_pct_trunk=parse_float(row['Body fat (%) - trunk']),
            heart_rate=parse_float(row.get('Heart rate', '0')) or None,
        )
        measurements.append(m)

    measurements.sort(key=lambda x: x.date)
    return measurements


def _age_from_dob(dob_str: str) -> int:
    """Calcula edad a partir de date_of_birth (YYYY-MM-DD)."""
    from datetime import date
    try:
        d = dob_str[:10].split('-')
        dob = date(int(d[0]), int(d[1]), int(d[2]))
        today = date.today()
        return today.year - dob.year - (
            (today.month, today.day) < (dob.month, dob.day)
        )
    except Exception:
        return 0


def _build_patient_info(patient_row: dict) -> PatientInfo:
    """Construye PatientInfo desde una fila de la tabla patients."""
    sex_raw = (patient_row.get('sex') or 'F').upper()
    # Normalizar variantes posibles
    if sex_raw in ('M', 'MALE', 'MASCULINO'):
        sex = 'M'
    else:
        sex = 'F'

    return PatientInfo(
        name=patient_row.get('full_name') or '',
        age=_age_from_dob(patient_row.get('date_of_birth') or ''),
        sex=sex,
        height_cm=float(patient_row.get('height_cm') or 170),
    )


def _get_nutri_signature(db, nutri_id: str) -> str:
    """Obtiene display_signature o full_name del nutri."""
    try:
        nutri = db.get_nutri(nutri_id)
        if not nutri:
            return ''
        return nutri.get('display_signature') or nutri.get('full_name') or ''
    except Exception:
        return ''


async def _regenerate_one(db, report_row: dict, dry_run: bool) -> dict:
    """
    Regenera UN reporte desde csv_raw. Si dry_run=False, sube el PDF
    a Storage sobrescribiendo el archivo existente.

    Returns: dict con resultado del regen (ok, report_id, error?, html_len?, pdf_size_kb?)
    """
    report_id = report_row['id']
    nutri_id = report_row['nutri_id']
    patient_id = report_row['patient_id']
    csv_raw = report_row.get('csv_raw') or ''

    out = {
        'report_id': report_id,
        'nutri_id': nutri_id,
        'patient_id': patient_id,
        'measurement_date': report_row.get('measurement_date'),
    }

    if not csv_raw.strip():
        out['ok'] = False
        out['error'] = 'csv_raw_empty'
        return out

    # Cargar patient
    patient_row = db.get_patient(patient_id)
    if not patient_row:
        out['ok'] = False
        out['error'] = 'patient_not_found'
        return out

    try:
        measurements = _parse_csv_string(csv_raw)
    except Exception as e:
        out['ok'] = False
        out['error'] = f'csv_parse_error: {e}'
        return out

    if not measurements:
        out['ok'] = False
        out['error'] = 'no_measurements_in_csv'
        return out

    patient = _build_patient_info(patient_row)
    doctor = _get_nutri_signature(db, nutri_id)

    # Filtrar mediciones hasta la fecha del reporte original — así
    # la "última" del análisis es la que se uso en su momento, no una
    # posterior que pudiera haber aparecido en una sincronización.
    target_date = (report_row.get('measurement_date') or '')[:10]
    if target_date:
        before = [m for m in measurements if m.date[:10] <= target_date]
        if before:
            measurements = before

    try:
        analysis = analyze(patient, measurements)
    except Exception as e:
        out['ok'] = False
        out['error'] = f'analyze_error: {e}'
        return out

    try:
        html = generate_html(analysis, doctor_name=doctor)
    except Exception as e:
        out['ok'] = False
        out['error'] = f'html_error: {e}'
        return out

    out['html_len'] = len(html)

    if dry_run:
        out['ok'] = True
        out['dry_run'] = True
        return out

    # Producción: generar PDF y sobrescribir en Storage
    try:
        from pipeline_v2 import generate_pdf_bytes
        pdf_bytes = await generate_pdf_bytes(html)
    except Exception as e:
        out['ok'] = False
        out['error'] = f'pdf_error: {e}'
        return out

    out['pdf_size_kb'] = len(pdf_bytes) // 1024

    try:
        # upload_pdf/upload_html ya usan upsert=true, así que sobrescriben
        db.upload_pdf(nutri_id, report_id, pdf_bytes)
        db.upload_html(nutri_id, report_id, html)
    except Exception as e:
        out['ok'] = False
        out['error'] = f'storage_upload_error: {e}'
        return out

    out['ok'] = True
    out['dry_run'] = False
    return out


# ─────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────

class RegenerateBatchRequest(BaseModel):
    dry_run: bool = True
    limit: Optional[int] = None
    nutri_id: Optional[str] = None  # filtrar por un nutri puntual


class RegenerateBatchResponse(BaseModel):
    total: int
    ok: int
    failed: int
    dry_run: bool
    results: list


# ─────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────

def register_routes(app, get_admin_dependency):
    """
    Registra el router en la app principal. Se llama desde api/main.py
    pasando la dependencia get_admin_nutri para que use el mismo auth
    que el resto de endpoints admin.
    """

    @router.post("/{report_id}/regenerate")
    async def regenerate_single(
        report_id: str,
        dry_run: bool = Query(True, description="Si true, no toca Storage"),
        admin_id: str = Depends(get_admin_dependency),
    ):
        """
        Regenera UN reporte usando el csv_raw guardado. No re-scrapea.
        Si dry_run=true (default), devuelve el HTML para inspección.
        Si dry_run=false, sobrescribe el PDF en Supabase Storage.
        """
        from db import DB
        db = DB()

        res = (db.client.table('reports')
               .select('id, nutri_id, patient_id, measurement_date, csv_raw, pdf_storage_path')
               .eq('id', report_id)
               .limit(1)
               .execute())

        if not res.data:
            raise HTTPException(status_code=404, detail="Reporte no encontrado")

        report_row = res.data[0]
        result = await _regenerate_one(db, report_row, dry_run)

        if dry_run and result.get('ok'):
            # Devolver el HTML directo al admin para inspección visual
            patient_row = db.get_patient(report_row['patient_id'])
            measurements = _parse_csv_string(report_row['csv_raw'])
            target_date = (report_row.get('measurement_date') or '')[:10]
            if target_date:
                before = [m for m in measurements if m.date[:10] <= target_date]
                if before:
                    measurements = before
            patient = _build_patient_info(patient_row)
            doctor = _get_nutri_signature(db, report_row['nutri_id'])
            analysis = analyze(patient, measurements)
            html = generate_html(analysis, doctor_name=doctor)

            _logger.info(
                "admin_regenerate dry_run report_id=%s admin=%s html_len=%d",
                report_id, admin_id, len(html),
            )
            return Response(
                content=html,
                media_type='text/html; charset=utf-8',
            )

        _logger.info(
            "admin_regenerate report_id=%s admin=%s ok=%s dry_run=%s error=%s",
            report_id, admin_id, result.get('ok'), dry_run, result.get('error'),
        )

        if not result.get('ok'):
            raise HTTPException(
                status_code=500,
                detail=result.get('error', 'unknown_error'),
            )

        return result

    @router.post("/regenerate-batch", response_model=RegenerateBatchResponse)
    async def regenerate_batch(
        body: RegenerateBatchRequest,
        admin_id: str = Depends(get_admin_dependency),
    ):
        """
        Regenera TODOS los reportes existentes (o filtrados por nutri_id).
        Por defecto dry_run=true → solo reporta qué pasaría sin tocar Storage.

        Útil después de un fix en pdf_generator para actualizar reportes ya
        entregados a nutris sin pedirles que vuelvan a generarlos.
        """
        from db import DB
        db = DB()

        q = (db.client.table('reports')
             .select('id, nutri_id, patient_id, measurement_date, csv_raw'))

        if body.nutri_id:
            q = q.eq('nutri_id', body.nutri_id)

        q = q.order('generated_at', desc=False)
        if body.limit:
            q = q.limit(body.limit)

        res = q.execute()
        reports = res.data or []

        results = []
        ok_count = 0
        failed_count = 0

        for row in reports:
            r = await _regenerate_one(db, row, body.dry_run)
            # Reducir payload: no devolver html_len para todos
            slim = {
                'report_id': r['report_id'],
                'nutri_id': r.get('nutri_id'),
                'measurement_date': r.get('measurement_date'),
                'ok': r.get('ok', False),
            }
            if not r.get('ok'):
                slim['error'] = r.get('error')
                failed_count += 1
            else:
                ok_count += 1
                if not body.dry_run:
                    slim['pdf_size_kb'] = r.get('pdf_size_kb')
            results.append(slim)

        _logger.info(
            "admin_regenerate_batch admin=%s total=%d ok=%d failed=%d dry_run=%s nutri_filter=%s",
            admin_id, len(reports), ok_count, failed_count,
            body.dry_run, body.nutri_id,
        )

        return RegenerateBatchResponse(
            total=len(reports),
            ok=ok_count,
            failed=failed_count,
            dry_run=body.dry_run,
            results=results,
        )

    app.include_router(router)

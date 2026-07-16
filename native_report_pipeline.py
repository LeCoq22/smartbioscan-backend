"""Generación de reportes SmartBioScan desde capturas BLE Nativa."""

import time
import uuid
from datetime import date, datetime

from fastapi import HTTPException

from analysis_engine import PatientInfo, analyze
from native_measurement_adapter import (
    NativeMeasurementDataError,
    native_row_to_tanita_measurement,
)
from pdf_generator_v2 import generate_html_v2 as generate_html
from pipeline_v2 import _measurement_is_complete_enough, generate_pdf_bytes


def _age_on(birth_date: str, captured_at: str) -> int:
    born = date.fromisoformat(str(birth_date)[:10])
    measured = datetime.fromisoformat(
        str(captured_at).replace("Z", "+00:00")
    ).date()
    return measured.year - born.year - (
        (measured.month, measured.day) < (born.month, born.day)
    )


async def run_native_report_pipeline(
    measurement_id: str,
    nutri_id: str,
    *,
    db=None,
) -> dict:
    """Analiza una medición persistida sin login, Playwright ni CSV."""
    if db is None:
        from db import DB
        db = DB()

    existing = db.get_report_by_native_measurement(measurement_id, nutri_id)
    if existing:
        return {"ok": True, "skipped": True, "report_id": str(existing["id"])}

    target = db.get_owned_native_measurement(measurement_id, nutri_id)
    if not target:
        raise HTTPException(status_code=404, detail="Medición nativa no encontrada")

    patient_id = str(target["patient_id"])
    patient_row = db.get_patient(patient_id)
    if not patient_row or str(patient_row.get("nutri_id")) != str(nutri_id):
        raise HTTPException(status_code=404, detail="Paciente no encontrado")

    quota = db.can_generate_report(nutri_id)
    if not quota.get("ok"):
        raise HTTPException(status_code=429, detail="Quota de reportes agotada")

    snapshot = target.get("profile_snapshot") or {}
    try:
        patient = PatientInfo(
            name=patient_row.get("full_name") or snapshot["scale_label"],
            age=_age_on(snapshot["birth_date"], target["captured_at"]),
            sex="F" if snapshot["sex"] == "female" else "M",
            height_cm=float(snapshot["height_cm"]),
        )
        rows = db.get_native_measurements_up_to(
            patient_id, nutri_id, target["captured_at"]
        )
        measurements = [native_row_to_tanita_measurement(row) for row in rows]
    except (KeyError, TypeError, ValueError, NativeMeasurementDataError) as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "native_measurement_invalid", "message": str(exc)},
        ) from exc

    if not measurements or str(rows[-1]["id"]) != str(measurement_id):
        raise HTTPException(status_code=409, detail="Historial nativo inconsistente")
    ok, missing = _measurement_is_complete_enough(measurements[-1])
    if not ok:
        raise HTTPException(
            status_code=422,
            detail={"code": "measurement_incomplete", "missing_fields": missing},
        )

    started = time.time()
    analysis = analyze(patient, measurements)
    doctor_row = db.get_nutri(nutri_id) or {}
    doctor = doctor_row.get("display_signature") or doctor_row.get("full_name") or ""
    html = generate_html(analysis, doctor_name=doctor)
    pdf_bytes = await generate_pdf_bytes(html)

    report_id = str(uuid.uuid4())
    storage_path = db.upload_pdf(nutri_id, report_id, pdf_bytes)
    db.upload_html(nutri_id, report_id, html)
    latest = measurements[-1]
    report = db.create_report(
        patient_id=patient_id,
        nutri_id=nutri_id,
        measurement={
            "date": latest.date,
            "weight_kg": latest.weight_kg,
            "body_fat_pct": latest.body_fat_pct,
            "muscle_mass_kg": latest.muscle_mass_kg,
            "visceral_fat": latest.visceral_fat,
            "bmr_kcal": latest.bmr_kcal,
            "metabolic_age": latest.metabolic_age,
        },
        csv_raw=None,
        pdf_path=storage_path,
        generation_secs=round(time.time() - started, 2),
        report_id=report_id,
        source="nativa",
        native_measurement_id=measurement_id,
    )
    return {"ok": True, "skipped": False, "report_id": str(report["id"])}

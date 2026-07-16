from unittest.mock import AsyncMock, patch

import pytest

from native_report_pipeline import run_native_report_pipeline
from tests.test_native_measurement_adapter import _fields


NUTRI = "nutri-1"
PATIENT = "patient-1"
MEASUREMENT = "11111111-1111-4111-8111-111111111111"


class FakeDB:
    def __init__(self):
        self.created = []
        self.existing = None
        self.row = {
            "id": MEASUREMENT,
            "nutri_id": NUTRI,
            "patient_id": PATIENT,
            "captured_at": "2026-07-16T10:30:00+00:00",
            "parser_version": "b010-tanita-tags-v3",
            "decoded_fields": _fields(),
            "profile_snapshot": {
                "birth_date": "1990-05-20",
                "sex": "male",
                "height_cm": 178.0,
                "scale_label": "PACIENTE",
            },
        }

    def get_report_by_native_measurement(self, measurement_id, nutri_id):
        return self.existing

    def get_owned_native_measurement(self, measurement_id, nutri_id):
        return self.row if measurement_id == MEASUREMENT and nutri_id == NUTRI else None

    def get_patient(self, patient_id):
        return {"id": PATIENT, "nutri_id": NUTRI, "full_name": "Paciente Nativa"}

    def can_generate_report(self, nutri_id):
        return {"ok": True}

    def get_native_measurements_up_to(self, patient_id, nutri_id, captured_at):
        return [self.row]

    def get_nutri(self, nutri_id):
        return {"display_signature": "Lic. Prueba"}

    def upload_pdf(self, nutri_id, report_id, pdf_bytes):
        assert pdf_bytes == b"PDF"
        return f"{nutri_id}/{report_id}.pdf"

    def upload_html(self, nutri_id, report_id, html):
        return f"{nutri_id}/{report_id}.html"

    def create_report(self, **payload):
        self.created.append(payload)
        return {"id": payload["report_id"]}


@pytest.mark.asyncio
async def test_generates_report_without_scraping_and_links_measurement():
    db = FakeDB()
    with patch("native_report_pipeline.generate_html", return_value="<html/>"), \
         patch("native_report_pipeline.generate_pdf_bytes", AsyncMock(return_value=b"PDF")):
        result = await run_native_report_pipeline(MEASUREMENT, NUTRI, db=db)

    assert result["ok"] is True
    assert result["skipped"] is False
    assert db.created[0]["native_measurement_id"] == MEASUREMENT
    assert db.created[0]["source"] == "nativa"
    assert db.created[0]["csv_raw"] is None


@pytest.mark.asyncio
async def test_existing_native_report_is_idempotent():
    db = FakeDB()
    db.existing = {"id": "report-existing", "pdf_storage_path": "x.pdf"}
    result = await run_native_report_pipeline(MEASUREMENT, NUTRI, db=db)
    assert result == {"ok": True, "skipped": True, "report_id": "report-existing"}
    assert db.created == []

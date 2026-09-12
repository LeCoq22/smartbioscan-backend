from unittest.mock import AsyncMock

import pytest

import db as db_module
import pipeline_v2
from analysis_engine import TanitaMeasurement
import tanita_scraper
from tanita_scraper import extract_all_measurements


def test_same_day_cache_keeps_latest_measurement(monkeypatch):
    class FakeFrame:
        columns = ['Date']
        iloc = [object(), object(), object()]

        def __len__(self):
            return len(self.iloc)

    parsed_rows = iter([
        {'date': '2026-09-10', 'weight_kg': 63.25, 'body_fat_pct': 27.1},
        {'date': '2026-09-10', 'weight_kg': 62.9, 'body_fat_pct': 24.0},
        {'date': '2026-09-10', 'weight_kg': 62.95, 'body_fat_pct': 24.0},
    ])
    monkeypatch.setattr(
        tanita_scraper,
        'csv_row_to_dict',
        lambda row, columns: next(parsed_rows),
    )

    rows = extract_all_measurements(FakeFrame())

    assert len(rows) == 1
    assert rows[0]["date"] == "2026-09-10"
    assert rows[0]["weight_kg"] == 63.25
    assert rows[0]["body_fat_pct"] == 27.1


class RefreshDB:
    def __init__(self):
        self.updated_report = None
        self.created_report = None
        self.quota_checked = False

    def get_report_for_date(self, patient_id, measurement_date):
        return {"id": "existing-report", "patient_id": patient_id}

    def can_generate_report(self, nutri_id):
        self.quota_checked = True
        return {"ok": False, "reason": "limit"}

    def get_tanita_credentials(self, patient_id):
        return {"tanita_email": "patient@example.com", "tanita_password": "secret"}

    def get_patient(self, patient_id):
        return {
            "id": patient_id,
            "nutri_id": "nutri-1",
            "full_name": "Patricia Devia",
            "date_of_birth": "1981-03-05",
            "sex": "M",
            "height_cm": 164,
            "mytanita_profile_id": None,
        }

    def get_nutri(self, nutri_id):
        return {"full_name": "Natalia Fritz"}

    def patient_has_settings(self, patient_id):
        return True

    def get_patient_csvs_up_to_date(self, patient_id, target_date):
        return []

    def update_scrape_status(self, *args, **kwargs):
        return None

    def sync_patient_settings(self, patient_id, settings):
        return {
            **self.get_patient(patient_id),
            "sex": settings["gender"],
        }

    def upsert_patient_csvs(self, patient_id, nutri_id, measurements):
        return len(measurements)

    def upload_pdf(self, nutri_id, report_id, pdf_bytes):
        return f"{nutri_id}/{report_id}.pdf"

    def upload_html(self, nutri_id, report_id, html):
        return f"{nutri_id}/{report_id}.html"

    def update_report(self, **payload):
        self.updated_report = payload
        return {"id": payload["report_id"]}

    def create_report(self, **payload):
        self.created_report = payload
        return {"id": payload["report_id"]}

    def mark_csv_report_generated(self, *args):
        return None


@pytest.mark.asyncio
async def test_same_day_generation_refreshes_existing_report_without_quota(monkeypatch):
    fake_db = RefreshDB()
    measurement = TanitaMeasurement(
        date="2026-09-10 20:33:00",
        weight_kg=63.25,
        bmi=23.5,
        body_fat_pct=27.1,
        visceral_fat=4.5,
        muscle_mass_kg=43.75,
        muscle_quality=67,
        bone_mass_kg=2.3,
        bmr_kcal=1363,
        metabolic_age=29,
        body_water_pct=49.9,
        physique_rating=5,
        muscle_right_arm=2.1,
        muscle_left_arm=2.0,
        muscle_right_leg=7.1,
        muscle_left_leg=6.85,
        muscle_trunk=25.7,
        quality_right_arm=73,
        quality_left_arm=67,
        quality_right_leg=67,
        quality_left_leg=64,
        quality_trunk=0,
        fat_pct_right_arm=29.4,
        fat_pct_left_arm=31.8,
        fat_pct_right_leg=34.5,
        fat_pct_left_leg=35.1,
        fat_pct_trunk=21.5,
        heart_rate=146,
    )
    monkeypatch.setattr(db_module, "DB", lambda: fake_db)
    monkeypatch.setattr(
        pipeline_v2,
        "do_scrape",
        AsyncMock(return_value={
            "error": None,
            "patient_data": {
                "name": "Patricia Devia",
                "dob": "1981-03-05",
                "age": 45,
                "sex": "F",
                "height_cm": 164,
            },
            "csv_path": "/tmp/patricia.csv",
            "csv_content": "csv-current",
        }),
    )
    monkeypatch.setattr(pipeline_v2, "load_csv", lambda path: [measurement])
    monkeypatch.setattr(pipeline_v2, "analyze", lambda patient, rows: object())
    monkeypatch.setattr(pipeline_v2, "generate_html", lambda analysis, doctor_name: "<html />")
    monkeypatch.setattr(
        pipeline_v2,
        "generate_pdf_bytes",
        AsyncMock(return_value=b"PDF"),
    )
    monkeypatch.setattr(
        "tanita_scraper.parse_tanita_csv", lambda csv_content: object()
    )
    monkeypatch.setattr(
        "tanita_scraper.extract_all_measurements", lambda dataframe: [{"date": "2026-09-10"}]
    )

    result = await pipeline_v2.run_pipeline(
        email="",
        password="",
        patient_id="patient-1",
        nutri_id="nutri-1",
        measurement_date="2026-09-10",
    )

    assert result["ok"] is True
    assert result["refreshed"] is True
    assert result["report_id"] == "existing-report"
    assert fake_db.quota_checked is False
    assert fake_db.created_report is None
    assert fake_db.updated_report["measurement"]["weight_kg"] == 63.25

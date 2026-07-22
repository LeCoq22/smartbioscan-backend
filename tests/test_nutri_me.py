from unittest.mock import patch

import pytest

from api.main import get_nutri_me


NUTRI_ID = "11111111-1111-4111-8111-111111111111"


class _Query:
    def __init__(self, row):
        self.row = row
        self.selected = ""

    def select(self, fields):
        self.selected = fields
        return self

    def eq(self, field, value):
        assert field == "id"
        assert value == NUTRI_ID
        return self

    def single(self):
        return self

    def execute(self):
        return type("Response", (), {"data": self.row})()


class _Client:
    def __init__(self, row):
        self.query = _Query(row)

    def table(self, name):
        assert name == "nutris"
        return self.query


class _DB:
    def __init__(self, row):
        self.client = _Client(row)


@pytest.mark.asyncio
async def test_nutri_me_exposes_shared_report_usage_and_signature():
    row = {
        "id": NUTRI_ID,
        "email": "nutri@example.com",
        "full_name": "Diana Makk",
        "display_signature": "Lic. Diana Makk - Nutricionista",
        "subscription_status": "active",
        "subscription_type": "basic_monthly",
        "subscription_end": "2026-08-20",
        "subscription_next_billing_date": "2026-08-20",
        "max_reports_month": 30,
        "reports_this_month": 9,
        "reports_total": 45,
        "reports_month_reset": "2026-07-20",
        "max_patients": 50,
    }
    db = _DB(row)

    with patch("db.DB", return_value=db):
        result = await get_nutri_me(NUTRI_ID)

    assert result == row
    assert "display_signature" in db.client.query.selected
    assert "reports_this_month" in db.client.query.selected
    assert "reports_total" in db.client.query.selected
    assert "reports_month_reset" in db.client.query.selected

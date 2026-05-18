"""
Tests unitarios para api/subscriptions.py

Dependencias de test (instalar con pip):
    pip install pytest pytest-asyncio httpx

Correr:
    cd /path/to/smartbioscan-backend
    pytest tests/test_subscriptions.py -v
"""

# Nota: el conftest.py mockea 'db', 'mercadopago', etc. antes que este archivo.
# Los patches de DB usan "db.DB" (donde DB está definido) porque subscriptions.py
# usa `from db import DB` dentro de funciones (no a nivel de módulo).

import asyncio
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# ── App mínima para tests ──────────────────────────────────────────────────────

def _make_test_app(nutri_id: str = "test-nutri-123"):
    """Crea una FastAPI mínima con el router de subscriptions."""
    from api.subscriptions import register_routes

    app = FastAPI()

    async def fake_get_current_nutri():
        return nutri_id

    register_routes(app, fake_get_current_nutri)
    return app


# ── Fixtures ────────────────────────────────────────────────────────────────────

TEST_NUTRI_ID = "test-nutri-123"
TEST_PREAPPROVAL_ID = "preapproval-abc-456"


@pytest.fixture
def client():
    app = _make_test_app(TEST_NUTRI_ID)
    return TestClient(app)


# ── Test 1: init con plan válido ───────────────────────────────────────────────

def test_init_subscription_valid_plan(client):
    """POST /api/subscriptions/init con plan válido → 200 con init_point."""

    mock_db = MagicMock()
    mock_db.client.table.return_value.select.return_value.eq.return_value.single.return_value.execute.return_value.data = {
        "email": "nutri@test.com"
    }

    mock_sdk = MagicMock()
    mock_sdk.preapproval.return_value.create.return_value = {
        "status": 201,
        "response": {
            "id":         TEST_PREAPPROVAL_ID,
            "init_point": "https://www.mercadopago.com.ar/subscriptions/checkout?preapproval_plan_id=xxx",
        },
    }

    with (
        patch("api.subscriptions.PLANS", {
            "bioscan_basico_mensual": {
                "title":             "BioScan Básico — Mensual",
                "unit_price":        24500.0,
                "months":            1,
                "max_reports_month": 30,
                "max_patients":      15,
                "subscription_type": "monthly",
                "mp_plan_id":        "mp-plan-test-001",
            }
        }),
        patch("api.subscriptions._mp_sdk", return_value=mock_sdk),
        patch("db.DB", return_value=mock_db),
    ):
        resp = client.post("/api/subscriptions/init", json={"plan_id": "bioscan_basico_mensual"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["preapproval_id"] == TEST_PREAPPROVAL_ID
    assert "mercadopago" in body["init_point"]


# ── Test 2: init con plan inválido ────────────────────────────────────────────

def test_init_subscription_invalid_plan(client):
    """POST /api/subscriptions/init con plan_id inexistente → 400."""
    resp = client.post("/api/subscriptions/init", json={"plan_id": "plan_inexistente"})
    assert resp.status_code == 400
    assert "plan_id inválido" in resp.json()["detail"]


# ── Test 3: init con mp_plan_id vacío (plan no configurado) ───────────────────

def test_init_subscription_plan_not_configured(client):
    """Si mp_plan_id está vacío, devuelve 503."""
    with patch("api.subscriptions.PLANS", {
        "bioscan_basico_mensual": {
            "title":             "BioScan Básico — Mensual",
            "unit_price":        24500.0,
            "months":            1,
            "max_reports_month": 30,
            "max_patients":      15,
            "subscription_type": "monthly",
            "mp_plan_id":        "",  # vacío — aún no configurado
        }
    }):
        resp = client.post("/api/subscriptions/init", json={"plan_id": "bioscan_basico_mensual"})

    assert resp.status_code == 503


# ── Test 4: webhook subscription_preapproval authorized ───────────────────────

@pytest.mark.asyncio
async def test_handle_subscription_preapproval_authorized():
    """Webhook subscription_preapproval + authorized → nutri actualizado."""
    from api.subscriptions import handle_subscription_preapproval, PLANS

    mock_sdk = MagicMock()
    mock_sdk.preapproval.return_value.get.return_value = {
        "status": 200,
        "response": {
            "id":                  TEST_PREAPPROVAL_ID,
            "status":              "authorized",
            "external_reference":  TEST_NUTRI_ID,
            "preapproval_plan_id": "mp-plan-test-001",
            "reason":              "BioScan Básico — Mensual",
            "payer_id":            "payer-999",
            "next_payment_date":   "2026-06-18T03:00:00.000-03:00",
        },
    }

    mock_db = MagicMock()
    mock_db.client.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [{
        "id":                  TEST_NUTRI_ID,
        "mp_preapproval_id":   None,
        "subscription_status": "expired",
        "subscription_end":    "2026-04-01",
    }]

    update_mock = MagicMock()
    mock_db.client.table.return_value.update.return_value.eq.return_value.execute = update_mock

    test_plans = {
        "bioscan_basico_mensual": {
            "title":             "BioScan Básico — Mensual",
            "months":            1,
            "max_reports_month": 30,
            "max_patients":      15,
            "subscription_type": "monthly",
            "mp_plan_id":        "mp-plan-test-001",
        }
    }

    with (
        patch("api.subscriptions.PLANS", test_plans),
        patch("db.DB", return_value=mock_db),
    ):
        result = await handle_subscription_preapproval(mock_sdk, TEST_PREAPPROVAL_ID)

    assert result["ok"] is True
    assert result["nutri_id"] == TEST_NUTRI_ID
    assert "subscription_end" in result

    # Verificar que se llamó update
    mock_db.client.table.return_value.update.assert_called_once()
    updated_data = mock_db.client.table.return_value.update.call_args[0][0]
    assert updated_data["subscription_status"] == "active"
    assert updated_data["mp_preapproval_id"] == TEST_PREAPPROVAL_ID
    assert updated_data["max_reports_month"] == 30


# ── Test 5: webhook duplicado (idempotencia) ──────────────────────────────────

@pytest.mark.asyncio
async def test_handle_subscription_preapproval_idempotent():
    """Webhook duplicado: mismo preapproval_id + ya activo → no procesa dos veces."""
    from api.subscriptions import handle_subscription_preapproval

    mock_sdk = MagicMock()
    mock_sdk.preapproval.return_value.get.return_value = {
        "status": 200,
        "response": {
            "id":                  TEST_PREAPPROVAL_ID,
            "status":              "authorized",
            "external_reference":  TEST_NUTRI_ID,
            "preapproval_plan_id": "mp-plan-test-001",
            "reason":              "BioScan Básico — Mensual",
        },
    }

    mock_db = MagicMock()
    # Nutri ya tiene este preapproval y está activo
    mock_db.client.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [{
        "id":                  TEST_NUTRI_ID,
        "mp_preapproval_id":   TEST_PREAPPROVAL_ID,  # ya procesado
        "subscription_status": "active",              # ya activo
        "subscription_end":    "2026-06-18",
    }]

    with patch("db.DB", return_value=mock_db):
        result = await handle_subscription_preapproval(mock_sdk, TEST_PREAPPROVAL_ID)

    assert result["ok"] is True
    assert result["detail"] == "already_active"
    # No debe haber llamado a update
    mock_db.client.table.return_value.update.assert_not_called()


# ── Test 6: cancel sin suscripción activa ─────────────────────────────────────

def test_cancel_subscription_no_active(client):
    """POST /api/subscriptions/cancel sin suscripción → 404."""
    mock_db = MagicMock()
    mock_db.client.table.return_value.select.return_value.eq.return_value.single.return_value.execute.return_value.data = {
        "mp_preapproval_id":   None,
        "subscription_status": "expired",
        "subscription_end":    "2026-04-01",
    }

    with patch("db.DB", return_value=mock_db):
        resp = client.post("/api/subscriptions/cancel")

    assert resp.status_code == 404

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

    response = MagicMock()
    response.status_code = 201
    response.json.return_value = {
        "id": TEST_PREAPPROVAL_ID,
        "init_point": "https://www.mercadopago.com.ar/subscriptions/checkout?preapproval_id=xxx",
    }
    http_client = MagicMock()
    http_client.post = AsyncMock(return_value=response)
    http_context = MagicMock()
    http_context.__aenter__ = AsyncMock(return_value=http_client)
    http_context.__aexit__ = AsyncMock(return_value=None)

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
        patch.dict("os.environ", {"MP_ACCESS_TOKEN": "APP_USR-test"}),
        patch("api.subscriptions.httpx.AsyncClient", return_value=http_context),
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


# ── Test 3: init inline no requiere mp_plan_id ─────────────────────────────────

def test_init_subscription_inline_does_not_require_mp_plan_id(client):
    """Las suscripciones inline funcionan sin preapproval_plan_id."""
    mock_db = MagicMock()
    mock_db.client.table.return_value.select.return_value.eq.return_value.single.return_value.execute.return_value.data = {
        "email": "nutri@test.com"
    }
    response = MagicMock()
    response.status_code = 201
    response.json.return_value = {
        "id": TEST_PREAPPROVAL_ID,
        "init_point": "https://www.mercadopago.com.ar/subscriptions/checkout?preapproval_id=xxx",
    }
    http_client = MagicMock()
    http_client.post = AsyncMock(return_value=response)
    http_context = MagicMock()
    http_context.__aenter__ = AsyncMock(return_value=http_client)
    http_context.__aexit__ = AsyncMock(return_value=None)

    with (
        patch("api.subscriptions.PLANS", {
            "bioscan_basico_mensual": {
                "title":             "BioScan Básico — Mensual",
                "unit_price":        24500.0,
                "months":            1,
                "max_reports_month": 30,
                "max_patients":      15,
                "subscription_type": "monthly",
                "mp_plan_id":        "",
            }
        }),
        patch.dict("os.environ", {"MP_ACCESS_TOKEN": "APP_USR-test"}),
        patch("api.subscriptions.httpx.AsyncClient", return_value=http_context),
        patch("db.DB", return_value=mock_db),
    ):
        resp = client.post("/api/subscriptions/init", json={"plan_id": "bioscan_basico_mensual"})

    assert resp.status_code == 200
    assert resp.json()["preapproval_id"] == TEST_PREAPPROVAL_ID


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


# ── Cobros recurrentes ────────────────────────────────────────────────────────

def _authorized_payment_http(invoice: dict):
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = invoice
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=None)
    return context


@pytest.mark.asyncio
async def test_authorized_payment_uses_nested_payment_status_and_extends_renewal():
    """
    La API de facturas devuelve status=scheduled arriba y
    payment.status=approved. Debe procesar la renovación, no ignorarla.
    """
    from api.subscriptions import handle_authorized_payment

    payment_id = "6114264375"
    invoice = {
        "id": payment_id,
        "status": "scheduled",
        "date_created": "2026-07-24T12:19:11-03:00",
        "debit_date": "2026-07-24T12:19:11-03:00",
        "preapproval_id": TEST_PREAPPROVAL_ID,
        "payment": {"id": 19951521071, "status": "approved"},
    }
    mock_sdk = MagicMock()
    mock_sdk.preapproval.return_value.get.return_value = {
        "status": 200,
        "response": {
            "id": TEST_PREAPPROVAL_ID,
            "reason": "BioScan Plus — Mensual",
            "next_payment_date": "2026-08-24T12:19:11-03:00",
        },
    }
    mock_db = MagicMock()
    mock_db.client.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [{
        "id": TEST_NUTRI_ID,
        "subscription_status": "active",
        "subscription_type": "monthly",
        "subscription_start": "2026-06-24",
        "subscription_end": "2026-07-24",
        "subscription_next_billing_date": "2026-07-24T12:19:11-03:00",
    }]

    with (
        patch.dict("os.environ", {"MP_ACCESS_TOKEN": "APP_USR-test"}),
        patch("api.subscriptions.httpx.AsyncClient",
              return_value=_authorized_payment_http(invoice)),
        patch("api.subscriptions._payment_event_processed", return_value=False),
        patch("db.DB", return_value=mock_db),
    ):
        result = await handle_authorized_payment(mock_sdk, payment_id)

    assert result["detail"] == "renewal_extended"
    assert result["subscription_end"] == "2026-08-24"
    updated_data = mock_db.client.table.return_value.update.call_args[0][0]
    assert updated_data["subscription_status"] == "active"
    assert updated_data["subscription_end"] == "2026-08-24"
    assert updated_data["subscription_next_billing_date"] == "2026-08-24T12:19:11-03:00"
    assert updated_data["max_reports_month"] == 100
    assert "reports_this_month" not in updated_data
    assert "reports_month_reset" not in updated_data


@pytest.mark.asyncio
async def test_authorized_payment_initial_invoice_does_not_double_extend():
    """El cobro inicial ya cubierto por preapproval no añade otro período."""
    from api.subscriptions import handle_authorized_payment

    payment_id = "initial-invoice-1"
    invoice = {
        "id": payment_id,
        "status": "scheduled",
        "date_created": "2026-06-24T12:19:11-03:00",
        "debit_date": "2026-06-24T12:19:11-03:00",
        "preapproval_id": TEST_PREAPPROVAL_ID,
        "payment": {"id": 1001, "status": "approved"},
    }
    mock_sdk = MagicMock()
    mock_sdk.preapproval.return_value.get.return_value = {
        "status": 200,
        "response": {
            "id": TEST_PREAPPROVAL_ID,
            "reason": "BioScan Básico — Mensual",
            "next_payment_date": "2026-07-24T12:19:11-03:00",
        },
    }
    mock_db = MagicMock()
    mock_db.client.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [{
        "id": TEST_NUTRI_ID,
        "subscription_status": "active",
        "subscription_type": "monthly",
        "subscription_start": "2026-06-24",
        "subscription_end": "2026-07-24",
        "subscription_next_billing_date": "2026-07-24T12:19:11-03:00",
    }]

    with (
        patch.dict("os.environ", {"MP_ACCESS_TOKEN": "APP_USR-test"}),
        patch("api.subscriptions.httpx.AsyncClient",
              return_value=_authorized_payment_http(invoice)),
        patch("api.subscriptions._payment_event_processed", return_value=False),
        patch("db.DB", return_value=mock_db),
    ):
        result = await handle_authorized_payment(mock_sdk, payment_id)

    assert result["detail"] == "initial_payment_already_covered"
    assert result["subscription_end"] == "2026-07-24"


@pytest.mark.asyncio
async def test_authorized_payment_idempotency_uses_invoice_id_not_quota_reset():
    """Un reintento del mismo invoice no toca la suscripción ni el cupo."""
    from api.subscriptions import handle_authorized_payment

    payment_id = "renewal-invoice-duplicate"
    invoice = {
        "id": payment_id,
        "status": "scheduled",
        "debit_date": "2026-07-24T12:19:11-03:00",
        "preapproval_id": TEST_PREAPPROVAL_ID,
        "payment": {"id": 1002, "status": "approved"},
    }
    mock_sdk = MagicMock()
    mock_sdk.preapproval.return_value.get.return_value = {
        "status": 200,
        "response": {"id": TEST_PREAPPROVAL_ID},
    }
    mock_db = MagicMock()
    mock_db.client.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [{
        "id": TEST_NUTRI_ID,
        "subscription_status": "active",
        "subscription_type": "monthly",
        "subscription_start": "2026-06-24",
        "subscription_end": "2026-08-24",
        "subscription_next_billing_date": "2026-08-24T12:19:11-03:00",
    }]

    with (
        patch.dict("os.environ", {"MP_ACCESS_TOKEN": "APP_USR-test"}),
        patch("api.subscriptions.httpx.AsyncClient",
              return_value=_authorized_payment_http(invoice)),
        patch("api.subscriptions._payment_event_processed", return_value=True),
        patch("db.DB", return_value=mock_db),
    ):
        result = await handle_authorized_payment(mock_sdk, payment_id)

    assert result["detail"] == "already_processed"
    mock_db.client.table.return_value.update.assert_not_called()


# ── Test cancel sin suscripción activa ────────────────────────────────────────

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

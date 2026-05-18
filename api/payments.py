"""
SmartTanita — Módulo de pagos MercadoPago (Checkout Bricks)

# DEPRECATED — migrating to subscriptions, see api/subscriptions.py
# Este módulo se mantiene para compatibilidad con pagos en vuelo.
# El endpoint /payments/webhook sigue activo y ahora también maneja
# los eventos de Subscriptions (subscription_preapproval,
# subscription_authorized_payment) delegando en api/subscriptions.py.

POST /payments/create-preference  — DEPRECATED: crea preferencia Bricks
POST /payments/webhook            — activo: recibe todas las notifs MP
"""

import calendar
import hashlib
import hmac
import logging
import os
from datetime import date
from typing import Optional

import mercadopago
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, EmailStr

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/payments", tags=["payments"])


# ─────────────────────────────────────────────
# PLANES
# ─────────────────────────────────────────────

PLANS: dict[str, dict] = {
    "bioscan_basico_mensual": {
        "title": "BioScan Básico — Mensual",
        "unit_price": 24500.0,
        "months": 1,
        "max_reports_month": 30,
        "max_patients": 15,
        "subscription_type": "monthly",
    },
    "bioscan_plus_mensual": {
        "title": "BioScan Plus — Mensual",
        "unit_price": 55000.0,
        "months": 1,
        "max_reports_month": 100,
        "max_patients": 40,
        "subscription_type": "monthly",
    },
    "bioscan_basico_semestral": {
        "title": "BioScan Básico — Semestral",
        "unit_price": 122400.0,
        "months": 6,
        "max_reports_month": 30,
        "max_patients": 15,
        "subscription_type": "quarterly",  # tipo más cercano disponible en el enum
    },
    "bioscan_plus_semestral": {
        "title": "BioScan Plus — Semestral",
        "unit_price": 274800.0,
        "months": 6,
        "max_reports_month": 100,
        "max_patients": 40,
        "subscription_type": "quarterly",
    },
}


# ─────────────────────────────────────────────
# SCHEMAS
# ─────────────────────────────────────────────

class CreatePreferenceRequest(BaseModel):
    plan_id: str
    user_email: EmailStr

class CreatePreferenceResponse(BaseModel):
    init_point: str
    preference_id: str


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def _mp_sdk() -> mercadopago.SDK:
    token = os.getenv("MP_ACCESS_TOKEN")
    if not token:
        raise HTTPException(status_code=500, detail="MP_ACCESS_TOKEN no configurado")
    return mercadopago.SDK(token)


def _add_months(base: date, months: int) -> date:
    """Suma N meses a una fecha, ajustando al último día del mes si hace falta."""
    month = base.month - 1 + months
    year = base.year + month // 12
    month = month % 12 + 1
    day = min(base.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _verify_webhook_signature(
    x_signature: Optional[str],
    x_request_id: Optional[str],
    data_id: str,
) -> bool:
    """
    Verifica la firma HMAC-SHA256 del webhook de MercadoPago.
    Si MP_WEBHOOK_SECRET no está configurado, pasa en modo dev sin verificar.
    Formato del header: x-signature: ts=<ts>,v1=<hash>
    """
    secret = os.getenv("MP_WEBHOOK_SECRET")
    if not secret:
        return True

    if not x_signature:
        return False

    ts = ""
    received_hash = ""
    for part in x_signature.split(","):
        k, _, v = part.partition("=")
        if k.strip() == "ts":
            ts = v.strip()
        elif k.strip() == "v1":
            received_hash = v.strip()

    if not ts or not received_hash:
        return False

    manifest = f"id:{data_id};request-id:{x_request_id};ts:{ts};"
    expected = hmac.new(
        secret.encode(), manifest.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, received_hash)


# ─────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────

@router.post("/create-preference", response_model=CreatePreferenceResponse)
async def create_preference(body: CreatePreferenceRequest):
    """
    Crea una preferencia de pago en MercadoPago para el plan elegido.

    Request:  POST /payments/create-preference
              { "plan_id": "bioscan_basico_mensual", "user_email": "nutri@email.com" }

    Response: { "init_point": "https://www.mercadopago.com.ar/checkout/...", "preference_id": "..." }
    """
    plan = PLANS.get(body.plan_id)
    if not plan:
        raise HTTPException(
            status_code=400,
            detail=f"plan_id inválido. Válidos: {list(PLANS.keys())}",
        )

    frontend_url = os.getenv("FRONTEND_URL", "http://localhost:5173")
    backend_url = os.getenv("BACKEND_URL", "")

    preference_data = {
        "items": [
            {
                "title": plan["title"],
                "quantity": 1,
                "unit_price": plan["unit_price"],
                "currency_id": "ARS",
            }
        ],
        "payer": {
            "email": body.user_email,
        },
        "back_urls": {
            "success": f"{frontend_url}/planes?status=success",
            "failure": f"{frontend_url}/planes?status=failure",
            "pending": f"{frontend_url}/planes?status=pending",
        },
        "auto_return": "approved",
        "metadata": {
            "plan_id": body.plan_id,
            "user_email": body.user_email,
        },
        "notification_url": f"{backend_url}/payments/webhook",
    }

    sdk = _mp_sdk()
    result = sdk.preference().create(preference_data)
    response = result.get("response", {})

    if result.get("status") not in (200, 201):
        logger.error("Error MP create_preference: %s", response)
        raise HTTPException(
            status_code=502,
            detail=f"Error MercadoPago: {response.get('message', response)}",
        )

    return CreatePreferenceResponse(
        init_point=response["init_point"],
        preference_id=response["id"],
    )


@router.post("/webhook", status_code=200)
async def mp_webhook(
    request: Request,
    x_signature: Optional[str] = Header(None, alias="x-signature"),
    x_request_id: Optional[str] = Header(None, alias="x-request-id"),
):
    """
    Recibe notificaciones de MercadoPago (todos los tipos de evento).

    Tipos manejados:
    - payment                        → Bricks (DEPRECATED, compatibilidad)
    - subscription_preapproval       → Subscriptions: activación / cancelación
    - subscription_authorized_payment → Subscriptions: cobro recurrente
    """
    payload = await request.json()
    event_type = payload.get("type", "")
    data_id = str(payload.get("data", {}).get("id", ""))

    if not data_id:
        return {"ok": True, "detail": "no data.id"}

    if not _verify_webhook_signature(x_signature, x_request_id, data_id):
        raise HTTPException(status_code=401, detail="Firma del webhook inválida")

    sdk = _mp_sdk()

    # ── Suscripciones (Pre-aprobados) ─────────────────────────────────────
    if event_type == "subscription_preapproval":
        from api.subscriptions import handle_subscription_preapproval
        return await handle_subscription_preapproval(sdk, data_id)

    if event_type == "subscription_authorized_payment":
        from api.subscriptions import handle_authorized_payment
        return await handle_authorized_payment(sdk, data_id)

    # ── Pago único Bricks (DEPRECATED) ────────────────────────────────────
    if event_type != "payment":
        return {"ok": True, "detail": f"event_type={event_type} ignored"}

    payment_result = sdk.payment().get(data_id)
    payment = payment_result.get("response", {})

    if payment_result.get("status") != 200:
        logger.error("No se pudo obtener pago %s: %s", data_id, payment)
        raise HTTPException(status_code=502, detail="No se pudo consultar el pago en MP")

    status = payment.get("status")
    if status != "approved":
        logger.info("Pago %s status=%s — ignorado", data_id, status)
        return {"ok": True, "detail": f"status={status}"}

    metadata = payment.get("metadata", {})
    plan_id = metadata.get("plan_id")
    user_email = metadata.get("user_email") or payment.get("payer", {}).get("email")

    plan = PLANS.get(plan_id)
    if not plan:
        logger.error("plan_id desconocido en webhook: %s | pago=%s", plan_id, data_id)
        return {"ok": True, "detail": f"plan_id '{plan_id}' desconocido"}

    from db import DB
    db = DB()

    res = (
        db.client.table("nutris")
        .select("id, subscription_end, subscription_status")
        .eq("email", user_email)
        .execute()
    )
    if not res.data:
        logger.error("Nutri no encontrado para email=%s | pago=%s", user_email, data_id)
        return {"ok": True, "detail": "nutri no encontrado"}

    nutri = res.data[0]
    nutri_id = nutri["id"]

    current_end_str = nutri.get("subscription_end")
    if (
        current_end_str
        and nutri.get("subscription_status") == "active"
        and date.fromisoformat(current_end_str) > date.today()
    ):
        new_end = _add_months(date.fromisoformat(current_end_str), plan["months"])
    else:
        new_end = _add_months(date.today(), plan["months"])

    db.client.table("nutris").update({
        "subscription_status": "active",
        "subscription_type":   plan["subscription_type"],
        "subscription_start":  date.today().isoformat(),
        "subscription_end":    new_end.isoformat(),
        "max_reports_month":   plan["max_reports_month"],
        "max_patients":        plan["max_patients"],
    }).eq("id", nutri_id).execute()

    logger.info("Suscripción Bricks activada: nutri=%s plan=%s hasta=%s", nutri_id, plan_id, new_end)
    return {
        "ok": True,
        "nutri_id": nutri_id,
        "plan_id": plan_id,
        "subscription_end": new_end.isoformat(),
    }

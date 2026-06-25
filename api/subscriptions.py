"""
SmartBioScan — Módulo de Subscripciones (MercadoPago Preapproval / Pre-aprobados)

Reemplaza los Checkout Bricks de api/payments.py (DEPRECATED).
El webhook de MP sigue llegando a POST /payments/webhook — los handlers
de subscription_preapproval y subscription_authorized_payment están en
este módulo y son llamados desde payments.py.

Endpoints registrados:
  POST /api/subscriptions/init    — inicia una suscripción pre-aprobada
  POST /api/subscriptions/cancel  — cancela la suscripción activa del nutri
"""

import calendar
import logging
import os
import re
from datetime import date, datetime, timezone
from typing import Optional

import httpx
import mercadopago
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

logger = logging.getLogger("smartbioscan.subscriptions")

router = APIRouter(prefix="/api/subscriptions", tags=["subscriptions"])

_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


# ─────────────────────────────────────────────
# PLANES
# ─────────────────────────────────────────────
# mp_plan_id se llena ejecutando scripts/create_mp_plans.py antes del deploy.
# Hasta entonces, init_subscription devuelve 503.

PLANS: dict[str, dict] = {
    "bioscan_basico_mensual": {
        "title": "BioScan Básico — Mensual",
        "unit_price": 28500.0,
        "months": 1,
        "max_reports_month": 30,
        "max_patients": 15,
        "subscription_type": "monthly",
        "mp_plan_id": "2b21a1d0497b45c68637cccafdd5cc22",  # completar tras ejecutar create_mp_plans.py
    },
    "bioscan_plus_mensual": {
        "title": "BioScan Plus — Mensual",
        "unit_price": 65000.0,
        "months": 1,
        "max_reports_month": 100,
        "max_patients": 40,
        "subscription_type": "monthly",
        "mp_plan_id": "6b612b2d9b624fa29d3ad200284580d5",
    },
    "bioscan_basico_semestral": {
        "title": "BioScan Básico — Semestral",
        "unit_price": 142500.0,
        "months": 6,
        "max_reports_month": 30,
        "max_patients": 15,
        "subscription_type": "semestral",
        "mp_plan_id": "6e96b27b48f9439e8385aaf87d133511",
    },
    "bioscan_plus_semestral": {
        "title": "BioScan Plus — Semestral",
        "unit_price": 325000.0,
        "months": 6,
        "max_reports_month": 100,
        "max_patients": 40,
        "subscription_type": "semestral",
        "mp_plan_id": "095f5bf5100d4a39a5d5cafd6062d94d",
    },
}


# ─────────────────────────────────────────────
# SCHEMAS
# ─────────────────────────────────────────────

class InitSubscriptionRequest(BaseModel):
    plan_id: str
    payer_email: Optional[str] = None   # email de la cuenta MP, si difiere del email del nutri


class InitSubscriptionResponse(BaseModel):
    init_point: str
    preapproval_id: str


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def _mp_sdk() -> mercadopago.SDK:
    token = os.getenv("MP_ACCESS_TOKEN")
    if not token:
        raise HTTPException(status_code=500, detail="MP_ACCESS_TOKEN no configurado")
    return mercadopago.SDK(token)


def _add_months(base: date, months: int) -> date:
    month = base.month - 1 + months
    year = base.year + month // 12
    month = month % 12 + 1
    day = min(base.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _find_plan_by_mp_plan_id(mp_plan_id: str) -> Optional[tuple[str, dict]]:
    for key, plan in PLANS.items():
        if plan.get("mp_plan_id") and plan["mp_plan_id"] == mp_plan_id:
            return key, plan
    return None


def _find_plan_by_reason(reason: str) -> Optional[tuple[str, dict]]:
    reason_lower = (reason or "").lower()
    for key, plan in PLANS.items():
        if plan["title"].lower() == reason_lower:
            return key, plan
    return None


# ─────────────────────────────────────────────
# WEBHOOK HANDLERS (llamados desde payments.py)
# ─────────────────────────────────────────────

async def handle_subscription_preapproval(sdk: mercadopago.SDK, preapproval_id: str) -> dict:
    """
    Maneja el evento webhook type=subscription_preapproval.
    data.id = preapproval_id

    Acción:
    - authorized → activa la suscripción del nutri
    - cancelled / paused → marca como cancelled (mantiene subscription_end)
    """
    result = sdk.preapproval().get(preapproval_id)
    if result.get("status") != 200:
        logger.error("preapproval.get(%s) failed: %s", preapproval_id, result.get("response"))
        return {"ok": True, "detail": "mp_api_error"}

    preapproval = result.get("response", {})
    status = preapproval.get("status")
    external_reference = preapproval.get("external_reference")  # = nutri_id
    mp_plan_id = preapproval.get("preapproval_plan_id", "")
    mp_payer_id = str(preapproval.get("payer_id") or "")
    next_billing = preapproval.get("next_payment_date")

    if not external_reference:
        logger.error("subscription_preapproval %s: sin external_reference", preapproval_id)
        return {"ok": True, "detail": "no_external_reference"}

    from db import DB
    db = DB()

    res = (db.client.table("nutris")
           .select("id, mp_preapproval_id, subscription_status, subscription_end")
           .eq("id", external_reference)
           .execute())

    if not res.data:
        logger.error("subscription_preapproval: nutri not found external_reference=%s", external_reference)
        return {"ok": True, "detail": "nutri_not_found"}

    nutri = res.data[0]
    nutri_id = nutri["id"]

    if status == "authorized":
        # Idempotencia: si ya está activo con este preapproval, ignorar
        if nutri.get("mp_preapproval_id") == preapproval_id and nutri.get("subscription_status") == "active":
            logger.info("subscription_preapproval %s ya activo — skip", preapproval_id)
            return {"ok": True, "detail": "already_active"}

        # Buscar plan por mp_plan_id o por reason (title)
        match = _find_plan_by_mp_plan_id(mp_plan_id) or _find_plan_by_reason(preapproval.get("reason", ""))
        if not match:
            logger.error(
                "subscription_preapproval: plan desconocido mp_plan_id=%s reason=%s",
                mp_plan_id, preapproval.get("reason"),
            )
            return {"ok": True, "detail": "plan_desconocido"}

        plan_key, plan = match
        subscription_start = date.today()
        subscription_end = _add_months(subscription_start, plan["months"])

        db.client.table("nutris").update({
            "subscription_status":             "active",
            "subscription_type":               plan["subscription_type"],
            "subscription_start":              subscription_start.isoformat(),
            "subscription_end":                subscription_end.isoformat(),
            "subscription_next_billing_date":  next_billing,
            "subscription_cancelled_at":       None,
            "mp_preapproval_id":               preapproval_id,
            "mp_payer_id":                     mp_payer_id or None,
            "max_reports_month":               plan["max_reports_month"],
            "max_patients":                    plan["max_patients"],
            # Estrena el cupo del mes al pagar: no arrastra reportes de la beta/trial.
            "reports_this_month":              0,
            "reports_month_reset":             subscription_start.isoformat(),
        }).eq("id", nutri_id).execute()

        logger.info(
            "subscription authorized: nutri=%s plan=%s preapproval=%s end=%s",
            nutri_id, plan_key, preapproval_id, subscription_end,
        )
        return {
            "ok": True,
            "nutri_id": nutri_id,
            "plan": plan_key,
            "subscription_end": subscription_end.isoformat(),
        }

    elif status in ("cancelled", "paused"):
        db.client.table("nutris").update({
            "subscription_status": "cancelled",
            # subscription_end NO se toca: acceso hasta que venza
        }).eq("id", nutri_id).execute()

        logger.info(
            "subscription_preapproval %s: nutri=%s status=%s",
            preapproval_id, nutri_id, status,
        )
        return {"ok": True, "nutri_id": nutri_id, "detail": f"subscription_{status}"}

    else:
        logger.info("subscription_preapproval %s status=%s — sin acción", preapproval_id, status)
        return {"ok": True, "detail": f"status={status}"}


async def handle_authorized_payment(sdk: mercadopago.SDK, authorized_payment_id: str) -> dict:
    """
    Maneja el evento webhook type=subscription_authorized_payment.
    data.id = authorized_payment_id (cobro recurrente exitoso)

    Acción:
    - approved → extiende subscription_end, resetea reports_this_month
    - rejected → solo loggear (MP reintenta solo)
    """
    token = os.getenv("MP_ACCESS_TOKEN")
    if not token:
        logger.error("MP_ACCESS_TOKEN no configurado para authorized_payment")
        return {"ok": True, "detail": "mp_token_missing"}

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"https://api.mercadopago.com/authorized_payments/{authorized_payment_id}",
            headers={"Authorization": f"Bearer {token}"},
        )

    if resp.status_code != 200:
        logger.error(
            "authorized_payment.get(%s) HTTP %s: %s",
            authorized_payment_id, resp.status_code, resp.text[:500],
        )
        return {"ok": True, "detail": "mp_api_error"}

    payment = resp.json()
    status = payment.get("status")
    preapproval_id = payment.get("preapproval_id")

    if status != "approved":
        logger.info(
            "authorized_payment %s status=%s — no action (MP reintenta solo)",
            authorized_payment_id, status,
        )
        return {"ok": True, "detail": f"status={status}"}

    if not preapproval_id:
        logger.error("authorized_payment %s: sin preapproval_id", authorized_payment_id)
        return {"ok": True, "detail": "no_preapproval_id"}

    from db import DB
    db = DB()

    res = (db.client.table("nutris")
           .select("id, subscription_status, subscription_end, subscription_type, reports_month_reset")
           .eq("mp_preapproval_id", preapproval_id)
           .execute())

    if not res.data:
        logger.error("authorized_payment: nutri not found for preapproval_id=%s", preapproval_id)
        return {"ok": True, "detail": "nutri_not_found"}

    nutri = res.data[0]
    nutri_id = nutri["id"]

    # Idempotencia: si reports_month_reset ya es hoy, asumimos que el cobro fue procesado
    reset_today = (nutri.get("reports_month_reset") or "")[:10] == date.today().isoformat()
    if reset_today and nutri.get("subscription_status") == "active":
        logger.info(
            "authorized_payment %s: ya procesado hoy — skip", authorized_payment_id,
        )
        return {"ok": True, "detail": "already_processed"}

    # Determinar cantidad de meses a extender según subscription_type del nutri
    subscription_type = nutri.get("subscription_type", "monthly")
    months = 6 if subscription_type == "semestral" else 1

    current_end_str = nutri.get("subscription_end")
    if current_end_str and nutri.get("subscription_status") == "active":
        base = date.fromisoformat(current_end_str[:10])
        new_end = _add_months(base, months)
    else:
        new_end = _add_months(date.today(), months)

    db.client.table("nutris").update({
        "subscription_status":  "active",
        "subscription_end":     new_end.isoformat(),
        "reports_this_month":   0,
        "reports_month_reset":  date.today().isoformat(),
        "subscription_next_billing_date": payment.get("next_payment_date"),
    }).eq("id", nutri_id).execute()

    logger.info(
        "authorized_payment approved: nutri=%s preapproval=%s authorized_payment=%s new_end=%s",
        nutri_id, preapproval_id, authorized_payment_id, new_end,
    )
    return {
        "ok": True,
        "nutri_id": nutri_id,
        "subscription_end": new_end.isoformat(),
    }


# ─────────────────────────────────────────────
# REGISTRO DE RUTAS (factory — igual que admin_regenerate.py)
# ─────────────────────────────────────────────

def register_routes(app, get_current_nutri_dep):
    """
    Registra el router en la app principal.
    Se llama desde api/main.py pasando la dependencia get_current_nutri.
    """

    @router.post("/init", response_model=InitSubscriptionResponse)
    async def init_subscription(
        body: InitSubscriptionRequest,
        nutri_id: str = Depends(get_current_nutri_dep),
    ):
        """
        Inicia una suscripción Pre-aprobada en MercadoPago.

        Request:  POST /api/subscriptions/init
                  Authorization: Bearer <jwt>
                  { "plan_id": "bioscan_basico_mensual" }

        Response: { "init_point": "https://www.mercadopago.com.ar/...", "preapproval_id": "..." }

        El frontend redirige al init_point. MP redirige de vuelta a /planes?status=pending.
        """
        plan = PLANS.get(body.plan_id)
        if not plan:
            raise HTTPException(
                status_code=400,
                detail=f"plan_id inválido. Opciones válidas: {list(PLANS.keys())}",
            )

        # Obtener email del nutri desde la BD
        from db import DB
        db = DB()
        nutri_res = (db.client.table("nutris")
                     .select("email")
                     .eq("id", nutri_id)
                     .single()
                     .execute())
        if not nutri_res.data:
            raise HTTPException(status_code=404, detail="Nutri no encontrado")

        payer_email = nutri_res.data["email"]
        # Si el nutri indicó el email de su cuenta de MercadoPago (porque difiere del
        # email de SmartBioScan), usamos ese para que MP no lo trabe en el checkout.
        # La reconciliación sigue por external_reference=nutri_id, así que el email NO
        # afecta a qué nutri se asocia el pago. Si llega basura, caemos al email del nutri.
        if body.payer_email:
            candidate = body.payer_email.strip().lower()
            if _EMAIL_RE.match(candidate):
                payer_email = candidate
            else:
                logger.warning(
                    "payer_email inválido recibido para nutri=%s — usando email del nutri",
                    nutri_id,
                )

        frontend_url = os.getenv("FRONTEND_URL", "http://localhost:5173")

        # En sandbox de MP, MP exige que payer_email sea de un test_user de su panel.
        # Override solo cuando estamos en TEST y la env var está seteada.
        mp_token = os.getenv("MP_ACCESS_TOKEN", "")
        test_email_override = os.getenv("MP_TEST_PAYER_EMAIL_OVERRIDE", "")
        if mp_token.startswith("TEST-") and test_email_override:
            logger.warning(
                "Usando MP_TEST_PAYER_EMAIL_OVERRIDE='%s' (sandbox) en lugar del email real='%s'",
                test_email_override, payer_email,
            )
            payer_email = test_email_override

        # Suscripción SIN preapproval_plan_id (Opción 2):
        # MP no exige card_token_id en este modo y devuelve init_point para Checkout Pro.
        # Los datos del plan van inline en auto_recurring.
        payload = {
            "reason":             plan["title"],
            "payer_email":        payer_email,
            "back_url":           f"{frontend_url}/planes?status=pending",
            "external_reference": nutri_id,
            "auto_recurring": {
                "frequency":          plan["months"],
                "frequency_type":     "months",
                "transaction_amount": plan["unit_price"],
                "currency_id":        "ARS",
            },
        }
        token = os.getenv("MP_ACCESS_TOKEN")
        if not token:
            raise HTTPException(status_code=500, detail="MP_ACCESS_TOKEN no configurado")

        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                "https://api.mercadopago.com/preapproval",
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type":  "application/json",
                },
            )

        if r.status_code not in (200, 201):
            logger.error(
                "preapproval HTTP create falló: nutri=%s plan=%s status=%s resp=%s",
                nutri_id, body.plan_id, r.status_code, r.text,
            )
            raise HTTPException(
                status_code=502,
                detail=f"Error MercadoPago: {r.text}",
            )

        response = r.json()
        preapproval_id = response.get("id", "")
        init_point = response.get("init_point", "")

        logger.info(
            "preapproval creado: nutri=%s plan=%s preapproval=%s",
            nutri_id, body.plan_id, preapproval_id,
        )

        return InitSubscriptionResponse(init_point=init_point, preapproval_id=preapproval_id)

    @router.post("/cancel")
    async def cancel_subscription(
        nutri_id: str = Depends(get_current_nutri_dep),
    ):
        """
        Cancela la suscripción activa del nutri autenticado.
        El acceso se mantiene hasta subscription_end.

        Response: { "ok": true, "cancelled_at": "...", "access_until": "..." }
        """
        from db import DB
        db = DB()

        nutri_res = (db.client.table("nutris")
                     .select("mp_preapproval_id, subscription_status, subscription_end")
                     .eq("id", nutri_id)
                     .single()
                     .execute())

        if not nutri_res.data:
            raise HTTPException(status_code=404, detail="Nutri no encontrado")

        nutri = nutri_res.data
        preapproval_id = nutri.get("mp_preapproval_id")

        if not preapproval_id:
            raise HTTPException(status_code=404, detail="No tenés una suscripción activa con pago automático")

        if nutri.get("subscription_status") == "cancelled":
            raise HTTPException(status_code=409, detail="La suscripción ya está cancelada")

        sdk = _mp_sdk()
        result = sdk.preapproval().update(preapproval_id, {"status": "cancelled"})

        if result.get("status") not in (200, 201):
            logger.error(
                "preapproval.update(cancel) falló: nutri=%s preapproval=%s resp=%s",
                nutri_id, preapproval_id, result.get("response"),
            )
            raise HTTPException(
                status_code=502,
                detail="No se pudo cancelar la suscripción en MercadoPago. Intentá de nuevo.",
            )

        cancelled_at = datetime.now(timezone.utc).isoformat()

        db.client.table("nutris").update({
            "subscription_status":        "cancelled",
            "subscription_cancelled_at":  cancelled_at,
            # subscription_end NO se modifica: acceso hasta que venza
        }).eq("id", nutri_id).execute()

        logger.info(
            "subscription cancelada: nutri=%s preapproval=%s access_until=%s",
            nutri_id, preapproval_id, nutri.get("subscription_end"),
        )

        return {
            "ok":           True,
            "cancelled_at": cancelled_at,
            "access_until": nutri.get("subscription_end"),
        }

    app.include_router(router)

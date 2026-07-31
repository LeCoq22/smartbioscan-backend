#!/usr/bin/env python3
"""
Reconciliación de renovaciones de Mercado Pago.

Busca suscripciones cuyo next_billing ya venció, consulta las facturas del
preapproval y reprocesa los cobros aprobados por el mismo handler del webhook.
Es dry-run por defecto; usar --apply en el job diario de producción.
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

import httpx
import mercadopago

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.subscriptions import handle_authorized_payment  # noqa: E402
from db import DB  # noqa: E402


def _is_approved(invoice: dict) -> bool:
    return (invoice.get("payment") or {}).get("status") == "approved"


async def reconcile(apply: bool) -> dict:
    token = os.getenv("MP_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("MP_ACCESS_TOKEN no configurado")

    db = DB()
    now = datetime.now(timezone.utc).isoformat()
    due = (
        db.client.table("nutris")
        .select("id,mp_preapproval_id,subscription_next_billing_date")
        .not_.is_("mp_preapproval_id", "null")
        .lte("subscription_next_billing_date", now)
        .execute()
        .data
        or []
    )

    sdk = mercadopago.SDK(token)
    result = {
        "mode": "apply" if apply else "dry_run",
        "due_subscriptions": len(due),
        "approved_invoices": 0,
        "processed": 0,
        "errors": [],
    }

    async with httpx.AsyncClient(timeout=20.0) as client:
        for nutri in due:
            preapproval_id = nutri["mp_preapproval_id"]
            try:
                response = await client.get(
                    "https://api.mercadopago.com/authorized_payments/search",
                    params={"preapproval_id": preapproval_id, "limit": 100},
                    headers={"Authorization": f"Bearer {token}"},
                )
                response.raise_for_status()
                invoices = sorted(
                    (response.json().get("results") or []),
                    key=lambda item: item.get("debit_date")
                    or item.get("date_created")
                    or "",
                )
                due_date = str(
                    nutri.get("subscription_next_billing_date") or ""
                )[:10]
                approved = [
                    item for item in invoices
                    if _is_approved(item)
                    and str(
                        item.get("debit_date")
                        or item.get("date_created")
                        or ""
                    )[:10] >= due_date
                ]
                result["approved_invoices"] += len(approved)

                if apply:
                    for invoice in approved:
                        invoice_id = str(invoice["id"])
                        processed = await handle_authorized_payment(sdk, invoice_id)
                        if processed.get("ok"):
                            result["processed"] += 1
            except Exception as exc:
                result["errors"].append({
                    "preapproval_id": preapproval_id,
                    "error": str(exc)[:300],
                })

    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Aplica las renovaciones; sin este flag solo informa.",
    )
    args = parser.parse_args()
    summary = asyncio.run(reconcile(args.apply))
    print(json.dumps(summary, indent=2))
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

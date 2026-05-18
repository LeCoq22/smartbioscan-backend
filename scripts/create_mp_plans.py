#!/usr/bin/env python3
"""
Crea los 4 preapproval_plans en MercadoPago.

Ejecutar UNA SOLA VEZ en ambiente de TEST, y luego otra vez en PRODUCCIÓN
con las credenciales de prod.

Uso:
    export MP_ACCESS_TOKEN=APP_USR-xxx...
    python scripts/create_mp_plans.py

Después de ejecutar: copiar los 4 IDs en el dict PLANS de api/subscriptions.py
y hacer push + deploy.
"""

import os
import sys

import mercadopago

# ── Verificar token ────────────────────────────────────────────────────────────
token = os.getenv("MP_ACCESS_TOKEN")
if not token:
    print("ERROR: MP_ACCESS_TOKEN no está en el entorno.")
    print("  Ejemplo: export MP_ACCESS_TOKEN=APP_USR-...")
    sys.exit(1)

sdk = mercadopago.SDK(token)

FRONTEND_URL = os.getenv("FRONTEND_URL", "https://app.smartbioscan.com")

# ── Definición de planes ───────────────────────────────────────────────────────
PLANS = [
    {
        "internal_key": "bioscan_basico_mensual",
        "reason":       "BioScan Básico — Mensual",
        "frequency":    1,
        "frequency_type": "months",
        "transaction_amount": 24500.0,  # cobro mensual
    },
    {
        "internal_key": "bioscan_plus_mensual",
        "reason":       "BioScan Plus — Mensual",
        "frequency":    1,
        "frequency_type": "months",
        "transaction_amount": 55000.0,
    },
    {
        "internal_key": "bioscan_basico_semestral",
        "reason":       "BioScan Básico — Semestral",
        "frequency":    6,
        "frequency_type": "months",
        "transaction_amount": 122400.0,  # cobro total cada 6 meses
    },
    {
        "internal_key": "bioscan_plus_semestral",
        "reason":       "BioScan Plus — Semestral",
        "frequency":    6,
        "frequency_type": "months",
        "transaction_amount": 274800.0,
    },
]


def create_plan(plan: dict) -> str:
    """Llama a la API de MP y retorna el id del plan creado."""
    payload = {
        "reason":     plan["reason"],
        "back_url":   f"{FRONTEND_URL}/planes?status=pending",
        "auto_recurring": {
            "frequency":                 plan["frequency"],
            "frequency_type":            plan["frequency_type"],
            "repetitions":               None,   # sin límite — recurrente indefinido
            "billing_day_proportional":  False,
            "transaction_amount":        plan["transaction_amount"],
            "currency_id":               "ARS",
        },
    }

    result = sdk.preapproval_plan().create(payload)
    response = result.get("response", {})

    if result.get("status") not in (200, 201):
        print(f"  ERROR: {response.get('message', response)}")
        sys.exit(1)

    return response["id"]


# ── Main ───────────────────────────────────────────────────────────────────────
print(f"Creando 4 preapproval_plans en MercadoPago...")
print(f"  Token: {token[:20]}...")
print(f"  FRONTEND_URL: {FRONTEND_URL}")
print()

created = {}
for plan in PLANS:
    key = plan["internal_key"]
    print(f"  Creando '{key}'...")
    mp_plan_id = create_plan(plan)
    created[key] = mp_plan_id
    print(f"    ✓ mp_plan_id = {mp_plan_id}")

print()
print("=" * 60)
print("Listo. Copiá estos IDs en el dict PLANS de api/subscriptions.py:")
print("=" * 60)
print()
for key, mp_id in created.items():
    print(f'    "{key}": {{')
    print(f'        ...')
    print(f'        "mp_plan_id": "{mp_id}",')
    print(f'    }},')
print()
print("Después hacé push y deploy.")

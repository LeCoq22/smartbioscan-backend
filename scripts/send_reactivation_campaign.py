"""
Campaña de reactivación: invita a nutris beta sin reportes a generar su primero
antes del 14 de junio.

Uso:
    # Modo dry-run (default) - solo muestra la lista, no manda nada
    python send_reactivation_campaign.py

    # Modo envío real - pide confirmación interactiva antes de mandar
    python send_reactivation_campaign.py --send

Requiere variables de entorno:
    SUPABASE_URL
    SUPABASE_SERVICE_KEY
    RESEND_API_KEY
"""
import os
import sys
import time
import argparse
from supabase import create_client
import resend


# ─────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────
FROM_EMAIL = "Diana Makk <equipo@mail.smartbioscan.com>"
REPLY_TO = "smartdiana@gmail.com"
SUBJECT = "Tu acceso beta a SmartBioScan vence el 14 de junio"
CTA_URL = "https://smartbioscan.com/app/patients/new"
DEADLINE = "14 de junio"
DELAY_BETWEEN_SENDS = 0.5  # segundos, para no pegarle al rate limit de Resend

# Excluir cuentas internas / tests
EXCLUDED_EMAILS = {
    "cremon@gmail.com",
    "cremon.arg@gmail.com",
    "cremon@duck.com",
    "cremonmd@gmail.com",
}

# Solo incluir nutris registrados antes de esta fecha (para no spamear muy nuevos)
CREATED_BEFORE = "2026-05-22 00:00:00+00"


# ─────────────────────────────────────────────────────────────────────
# TEMPLATE EMAIL (HTML + texto plano fallback)
# ─────────────────────────────────────────────────────────────────────
def build_html(full_name: str) -> str:
    first_name = full_name.strip().split()[0].title() if full_name else "hola"
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         color: #1f2937; line-height: 1.6; max-width: 600px; margin: 0 auto; padding: 24px; }}
  h2 {{ color: #111827; }}
  .cta {{ display: inline-block; background: #16a34a; color: #fff; padding: 12px 24px;
         border-radius: 6px; text-decoration: none; font-weight: 600; margin: 16px 0; }}
  ol li {{ margin-bottom: 8px; }}
  .footer {{ margin-top: 32px; font-size: 13px; color: #6b7280; }}
  .signature {{ margin-top: 24px; }}
  .signature strong {{ color: #111827; }}
  .signature .role {{ color: #6b7280; font-size: 14px; }}
</style>
</head>
<body>
  <p>Hola {first_name},</p>

  <p>Te escribo desde <strong>SmartBioScan</strong>. Vi que te registraste hace unos días
  pero todavía no generaste tu primer reporte, y quería asegurarme de que no se te haya
  complicado el setup.</p>

  <p>Tu acceso beta gratuito está activo hasta el <strong>{DEADLINE}</strong>. Después de
  esa fecha, los lugares de la beta van a ir rotando hacia las personas que están en lista
  de espera, así que si querés conservar tu acceso necesitás generar al menos un reporte
  esta semana.</p>

  <p>El proceso es más rápido de lo que parece — 3 pasos:</p>
  <ol>
    <li><strong>Cargá tu primer paciente</strong> desde Pacientes → Nuevo paciente
        (necesitás su email/contraseña de MyTanita y el ID del perfil donde se mide).</li>
    <li><strong>Generá el reporte</strong> desde la pantalla del paciente, botón
        "Generar reporte".</li>
    <li><strong>Descargá el PDF</strong> — vas a ver un informe mucho más completo que el
        de Tanita, con rangos de referencia, balance muscular y todas las métricas
        segmentales interpretadas.</li>
  </ol>

  <p>
    <a href="{CTA_URL}" class="cta">Cargar mi primer paciente</a>
  </p>

  <p>Si te trabaste en algún paso, respondeme este mail y lo resolvemos hoy mismo.</p>

  <div class="signature">
    Un abrazo,<br>
    <strong>Lic. Diana Makk</strong><br>
    <span class="role">SmartBioScan</span>
  </div>

  <div class="footer">
    Este correo es parte del programa beta de SmartBioScan.
  </div>
</body>
</html>
"""


def build_text(full_name: str) -> str:
    first_name = full_name.strip().split()[0].title() if full_name else "hola"
    return f"""Hola {first_name},

Te escribo desde SmartBioScan. Vi que te registraste hace unos días pero todavía
no generaste tu primer reporte, y quería asegurarme de que no se te haya complicado
el setup.

Tu acceso beta gratuito está activo hasta el {DEADLINE}. Después de esa fecha, los
lugares de la beta van a ir rotando hacia las personas que están en lista de espera,
así que si querés conservar tu acceso necesitás generar al menos un reporte esta semana.

El proceso es más rápido de lo que parece — 3 pasos:

1. Cargá tu primer paciente desde Pacientes → Nuevo paciente (necesitás su
   email/contraseña de MyTanita y el ID del perfil donde se mide).
2. Generá el reporte desde la pantalla del paciente, botón "Generar reporte".
3. Descargá el PDF — vas a ver un informe mucho más completo que el de Tanita,
   con rangos de referencia, balance muscular y todas las métricas segmentales
   interpretadas.

→ Cargar mi primer paciente: {CTA_URL}

Si te trabaste en algún paso, respondeme este mail y lo resolvemos hoy mismo.

Un abrazo,
Lic. Diana Makk
SmartBioScan
"""


# ─────────────────────────────────────────────────────────────────────
# LÓGICA
# ─────────────────────────────────────────────────────────────────────
def fetch_recipients(supabase) -> list[dict]:
    """Trae los nutris beta sin reportes que cumplen el criterio de la campaña."""
    response = (
        supabase
        .table("nutris")
        .select("id, full_name, email, created_at, reports_total")
        .eq("is_suspended", False)
        .eq("role", "user")
        .eq("reports_total", 0)
        .lt("created_at", CREATED_BEFORE)
        .order("created_at", desc=False)
        .execute()
    )
    rows = response.data or []
    return [r for r in rows if r["email"] not in EXCLUDED_EMAILS]


def send_one(recipient: dict) -> tuple[bool, str]:
    """Manda un email. Devuelve (ok, mensaje)."""
    try:
        params = {
            "from": FROM_EMAIL,
            "to": [recipient["email"]],
            "reply_to": REPLY_TO,
            "subject": SUBJECT,
            "html": build_html(recipient["full_name"]),
            "text": build_text(recipient["full_name"]),
            "tags": [
                {"name": "campaign", "value": "beta-reactivation-may2026"},
            ],
        }
        result = resend.Emails.send(params)
        return True, result.get("id", "ok")
    except Exception as e:
        return False, str(e)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--send",
        action="store_true",
        help="Envía de verdad. Sin esta flag corre en dry-run.",
    )
    args = parser.parse_args()

    # ---- setup clientes ----
    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_SERVICE_KEY")
    resend_key = os.environ.get("RESEND_API_KEY")

    missing = [
        name for name, val in [
            ("SUPABASE_URL", supabase_url),
            ("SUPABASE_SERVICE_KEY", supabase_key),
            ("RESEND_API_KEY", resend_key),
        ] if not val
    ]
    if missing:
        print(f"❌ Faltan variables de entorno: {', '.join(missing)}")
        sys.exit(1)

    supabase = create_client(supabase_url, supabase_key)
    resend.api_key = resend_key

    # ---- traer destinatarios ----
    print("📡 Consultando Supabase...")
    recipients = fetch_recipients(supabase)

    if not recipients:
        print("⚠️  No hay destinatarios que cumplan el criterio. Nada que enviar.")
        sys.exit(0)

    print(f"\n✉️  {len(recipients)} destinatarios encontrados:\n")
    print(f"{'#':>3}  {'Email':<45}  {'Nombre':<35}  Registrado")
    print("─" * 110)
    for i, r in enumerate(recipients, 1):
        created = r["created_at"][:10]
        name = (r["full_name"] or "")[:33]
        print(f"{i:>3}  {r['email']:<45}  {name:<35}  {created}")

    # ---- dry run ----
    if not args.send:
        print(f"\n🧪 DRY-RUN. Para enviar de verdad: python {sys.argv[0]} --send")
        return

    # ---- confirmación final ----
    print(f"\n⚠️  Vas a mandar {len(recipients)} emails REALES desde {FROM_EMAIL}.")
    confirm = input("¿Confirmás? Escribí EXACTAMENTE 'enviar' para proceder: ").strip()
    if confirm != "enviar":
        print("❌ Cancelado.")
        sys.exit(0)

    # ---- envío ----
    print(f"\n🚀 Enviando... (sleep {DELAY_BETWEEN_SENDS}s entre envíos)\n")
    ok_count = 0
    fail_count = 0
    failures = []

    for i, r in enumerate(recipients, 1):
        ok, msg = send_one(r)
        status = "✅" if ok else "❌"
        print(f"{status} [{i}/{len(recipients)}] {r['email']:<45}  {msg}")
        if ok:
            ok_count += 1
        else:
            fail_count += 1
            failures.append((r["email"], msg))
        time.sleep(DELAY_BETWEEN_SENDS)

    # ---- resumen ----
    print("\n" + "=" * 60)
    print(f"📊 RESUMEN")
    print(f"   Enviados OK:    {ok_count}")
    print(f"   Fallidos:       {fail_count}")
    print(f"   Total:          {len(recipients)}")
    if failures:
        print(f"\n❌ Fallos:")
        for email, err in failures:
            print(f"   - {email}: {err}")


if __name__ == "__main__":
    main()

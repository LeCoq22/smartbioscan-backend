"""
Campaña 2: nutris beta SIN PACIENTES cargados.

Invita a los nutris que se registraron pero no llegaron a cargar ni un solo
paciente a aprovechar los últimos días de la beta hasta el 14 de junio.

Uso:
    # Modo dry-run (default) - solo muestra la lista, no manda nada
    python send_no_patients_reminder.py

    # Modo envío real - pide confirmación interactiva antes de mandar
    python send_no_patients_reminder.py --send

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
SUBJECT = "Te quedan 10 días de prueba en SmartBioScan"
CTA_URL = "https://smartbioscan.com/app/patients/new"
DEADLINE = "14 de junio"
DELAY_BETWEEN_SENDS = 0.5

# Excluir cuentas internas / tests (mismo set que la campaña 1)
EXCLUDED_EMAILS = {
    "cremon@gmail.com",
    "cremon.arg@gmail.com",
    "cremon@duck.com",
    "cremonmd@gmail.com",
}

# Solo incluir nutris registrados antes de esta fecha (Claus pidió excluir
# a Mariana Bauducco que se registró el 01/06)
CREATED_BEFORE = "2026-05-30 00:00:00+00"


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

  <p>Te escribo desde <strong>SmartBioScan</strong>. Vi que te registraste pero todavía no
  cargaste ningún paciente, y quería avisarte que <strong>te quedan 10 días</strong> de
  prueba gratuita — la beta gratuita está activa hasta el <strong>{DEADLINE}</strong>.</p>

  <p>Ojalá puedas aprovechar estos últimos días para conocer la plataforma y obtener
  reportes de tus pacientes con bastante más información y mejor presentada que la que
  obtenés regularmente con la app de Tanita.</p>

  <p>El primer paciente lo cargás en 1 minuto:</p>
  <ol>
    <li>Andá a <strong>Pacientes → Nuevo paciente</strong>.</li>
    <li>Poné el nombre del paciente.</li>
    <li>Ingresá las credenciales de MyTanita donde está el perfil del paciente.</li>
    <li>Tocá <strong>Verificar credenciales</strong> y elegí el perfil que corresponde.</li>
    <li>Guardá. Listo — ya podés generar reportes con todas sus mediciones.</li>
  </ol>

  <p>
    <a href="{CTA_URL}" class="cta">Cargar mi primer paciente</a>
  </p>

  <p><strong>¿Tenés dudas o preguntas sobre cómo usar la plataforma?</strong><br>
  Respondé este mail o escribime por WhatsApp (tenés mi número), y te las contesto
  personalmente.</p>

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

Te escribo desde SmartBioScan. Vi que te registraste pero todavía no cargaste
ningún paciente, y quería avisarte que te quedan 10 días de prueba gratuita —
la beta gratuita está activa hasta el {DEADLINE}.

Ojalá puedas aprovechar estos últimos días para conocer la plataforma y obtener
reportes de tus pacientes con bastante más información y mejor presentada que
la que obtenés regularmente con la app de Tanita.

El primer paciente lo cargás en 1 minuto:

1. Andá a Pacientes → Nuevo paciente.
2. Poné el nombre del paciente.
3. Ingresá las credenciales de MyTanita donde está el perfil del paciente.
4. Tocá "Verificar credenciales" y elegí el perfil que corresponde.
5. Guardá. Listo — ya podés generar reportes con todas sus mediciones.

→ Cargar mi primer paciente: {CTA_URL}

¿Tenés dudas o preguntas sobre cómo usar la plataforma?
Respondé este mail o escribime por WhatsApp (tenés mi número), y te las
contesto personalmente.

Un abrazo,
Lic. Diana Makk
SmartBioScan
"""


# ─────────────────────────────────────────────────────────────────────
# LÓGICA
# ─────────────────────────────────────────────────────────────────────
def fetch_recipients(supabase) -> list[dict]:
    """Trae los nutris activos SIN pacientes cargados."""
    response = (
        supabase
        .table("nutris")
        .select("id, full_name, email, created_at")
        .eq("is_suspended", False)
        .eq("role", "user")
        .lt("created_at", CREATED_BEFORE)
        .order("created_at", desc=False)
        .execute()
    )
    rows = response.data or []

    # filtrar exclusiones
    rows = [r for r in rows if r["email"] not in EXCLUDED_EMAILS]

    # filtrar los que tienen al menos 1 paciente activo
    nutri_ids = [r["id"] for r in rows]
    if not nutri_ids:
        return []

    # query patients en chunks por las dudas (Supabase tiene límites)
    patients_resp = (
        supabase
        .table("patients")
        .select("nutri_id")
        .eq("is_active", True)
        .in_("nutri_id", nutri_ids)
        .execute()
    )
    with_patients = {p["nutri_id"] for p in (patients_resp.data or [])}

    return [r for r in rows if r["id"] not in with_patients]


def send_one(recipient: dict) -> tuple[bool, str]:
    try:
        params = {
            "from": FROM_EMAIL,
            "to": [recipient["email"]],
            "reply_to": REPLY_TO,
            "subject": SUBJECT,
            "html": build_html(recipient["full_name"]),
            "text": build_text(recipient["full_name"]),
            "tags": [
                {"name": "campaign", "value": "no-patients-reminder-jun2026"},
            ],
        }
        result = resend.Emails.send(params)
        return True, result.get("id", "ok")
    except Exception as e:
        return False, str(e)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--send", action="store_true",
                        help="Envía de verdad. Sin esta flag corre en dry-run.")
    args = parser.parse_args()

    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_SERVICE_KEY")
    resend_key = os.environ.get("RESEND_API_KEY")

    missing = [name for name, val in [
        ("SUPABASE_URL", supabase_url),
        ("SUPABASE_SERVICE_KEY", supabase_key),
        ("RESEND_API_KEY", resend_key),
    ] if not val]
    if missing:
        print(f"❌ Faltan variables de entorno: {', '.join(missing)}")
        sys.exit(1)

    supabase = create_client(supabase_url, supabase_key)
    resend.api_key = resend_key

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

    if not args.send:
        print(f"\n🧪 DRY-RUN. Para enviar de verdad: python {sys.argv[0]} --send")
        return

    print(f"\n⚠️  Vas a mandar {len(recipients)} emails REALES desde {FROM_EMAIL}.")
    confirm = input("¿Confirmás? Escribí EXACTAMENTE 'enviar' para proceder: ").strip()
    if confirm != "enviar":
        print("❌ Cancelado.")
        sys.exit(0)

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

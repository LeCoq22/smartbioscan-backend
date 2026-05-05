"""
api/email.py — Emails transaccionales via Resend

Ambas funciones capturan excepciones internamente: el flujo principal no
se interrumpe por un fallo de email. Retornan el id del email enviado o None.
"""

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

FROM_EMAIL = "Diana Makk <equipo@mail.smartbioscan.com>"
REPLY_TO   = "cremon@gmail.com"


def _get_resend():
    try:
        import resend as _r
        _r.api_key = os.environ.get("RESEND_API_KEY", "")
        return _r
    except ImportError:
        return None
    except Exception as exc:
        logger.error("Error al cargar módulo resend: %s", exc)
        return None


def send_waitlist_confirmation(to_email: str, nombre: str) -> Optional[str]:
    """Email A — Recibimos tu solicitud. Retorna email_id o None."""
    r = _get_resend()
    if not r:
        logger.warning("resend no instalado — Email A omitido para %s", to_email)
        return None
    if not r.api_key:
        logger.warning("RESEND_API_KEY vacía — Email A omitido para %s", to_email)
        return None
    try:
        resp = r.Emails.send({
            "from":     FROM_EMAIL,
            "reply_to": REPLY_TO,
            "to":       [to_email],
            "subject":  "Recibimos tu solicitud — SmartBioScan",
            "text":     _email_a_text(nombre),
            "html":     _email_a_html(nombre),
        })
        eid = resp.id if hasattr(resp, "id") else (resp.get("id") if isinstance(resp, dict) else None)
        logger.info("Email A enviado a %s id=%s", to_email, eid)
        return eid
    except Exception as exc:
        logger.error("Error Email A → %s: %s", to_email, exc)
        return None


def send_welcome_email(to_email: str, nombre: str, set_password_url: str) -> Optional[str]:
    """Email B — Tu cuenta está lista. Retorna email_id o None."""
    r = _get_resend()
    if not r:
        logger.warning("resend no instalado — Email B omitido para %s", to_email)
        return None
    if not r.api_key:
        logger.warning("RESEND_API_KEY vacía — Email B omitido para %s", to_email)
        return None
    try:
        resp = r.Emails.send({
            "from":     FROM_EMAIL,
            "reply_to": REPLY_TO,
            "to":       [to_email],
            "subject":  "Tu cuenta de SmartBioScan está lista",
            "text":     _email_b_text(nombre, set_password_url),
            "html":     _email_b_html(nombre, set_password_url),
        })
        eid = resp.id if hasattr(resp, "id") else (resp.get("id") if isinstance(resp, dict) else None)
        logger.info("Email B enviado a %s id=%s", to_email, eid)
        return eid
    except Exception as exc:
        logger.error("Error Email B → %s: %s", to_email, exc)
        return None


# ── Templates ─────────────────────────────────────────────────────────────────

def _email_a_text(nombre: str) -> str:
    return f"""Hola {nombre},

Recibimos tu solicitud para sumarte al beta de SmartBioScan. ¡Gracias por el interés!

Estamos arrancando con un grupo chico de profesionales para asegurar que la herramienta \
esté lo más pulida posible. Te vamos a contactar en los próximos días con tu acceso.

Mientras tanto, si tenés alguna pregunta, podés responder este correo.

Lic. Diana Makk
SmartBioScan"""


def _email_a_html(nombre: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Recibimos tu solicitud</title>
</head>
<body style="margin:0;padding:0;background-color:#f4f6f8;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f4f6f8;padding:32px 16px;">
  <tr><td align="center">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background-color:#ffffff;border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,0.08);">
      <tr><td style="padding:32px 32px 16px 32px;text-align:center;border-bottom:1px solid #eef0f3;">
        <h1 style="margin:0;font-size:20px;font-weight:600;color:#0d7377;">SmartBioScan</h1>
        <p style="margin:4px 0 0 0;font-size:13px;color:#6b7280;">Análisis de composición corporal</p>
      </td></tr>
      <tr><td style="padding:32px;">
        <p style="margin:0 0 16px 0;font-size:16px;color:#1f2937;line-height:1.5;">Hola <strong>{nombre}</strong>,</p>
        <p style="margin:0 0 16px 0;font-size:15px;color:#374151;line-height:1.6;">Recibimos tu solicitud para sumarte al beta de SmartBioScan. ¡Gracias por el interés!</p>
        <p style="margin:0 0 16px 0;font-size:15px;color:#374151;line-height:1.6;">Estamos arrancando con un grupo chico de profesionales para asegurar que la herramienta esté lo más pulida posible. Te vamos a contactar en los próximos días con tu acceso.</p>
        <p style="margin:0 0 24px 0;font-size:15px;color:#374151;line-height:1.6;">Mientras tanto, si tenés alguna pregunta, podés responder este correo.</p>
        <p style="margin:0;font-size:15px;color:#1f2937;line-height:1.5;"><strong>Lic. Diana Makk</strong><br><span style="color:#6b7280;font-size:14px;">SmartBioScan</span></p>
      </td></tr>
      <tr><td style="padding:20px 32px;background-color:#f9fafb;border-top:1px solid #eef0f3;border-radius:0 0 8px 8px;text-align:center;">
        <p style="margin:0;font-size:12px;color:#9ca3af;line-height:1.5;">Recibís este correo porque te registraste en el beta de SmartBioScan.<br>Si no esperabas este mensaje, podés ignorarlo.</p>
      </td></tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""


def _email_b_text(nombre: str, url: str) -> str:
    return f"""Hola {nombre},

Gracias por sumarte al beta de SmartBioScan. Te escribo personalmente porque sos parte \
del primer grupo de nutricionistas que va a usar la herramienta antes del lanzamiento público.

Te creé una cuenta. Antes de empezar, necesito que establezcas tu contraseña:

      [ ESTABLECER MI CONTRASEÑA ]
      {url}

(Este link es único y vence en 7 días.)

Una vez adentro, vas a poder:

  • Cargar pacientes desde sus datos de Tanita
  • Generar reportes de composición corporal en PDF, listos para entregar
  • Ver la evolución de tus pacientes a lo largo del tiempo

Tu cuenta tiene créditos suficientes para que pruebes la herramienta con calma.

Algunas cosas para tener en cuenta:

  • Estás usando una versión en desarrollo. Es posible que encuentres cosas que no funcionan \
como esperás. Cuando pase, escribime y lo arreglamos rápido.
  • El envío automático por WhatsApp todavía no está disponible. Por ahora podés descargar \
el PDF y enviarlo manualmente.
  • Lo que más necesito de vos es feedback honesto. Qué te resulta confuso, qué te falta, \
qué te molesta. Sin eso, esto no mejora.

Cualquier duda, respondé este mismo correo. Lo leo yo.

Gracias por confiar.

Lic. Diana Makk
SmartBioScan"""


def _email_b_html(nombre: str, url: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Tu cuenta de SmartBioScan está lista</title>
</head>
<body style="margin:0;padding:0;background-color:#f4f6f8;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f4f6f8;padding:32px 16px;">
  <tr><td align="center">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background-color:#ffffff;border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,0.08);">
      <tr><td style="padding:32px 32px 16px 32px;text-align:center;border-bottom:1px solid #eef0f3;">
        <h1 style="margin:0;font-size:20px;font-weight:600;color:#0d7377;">SmartBioScan</h1>
        <p style="margin:4px 0 0 0;font-size:13px;color:#6b7280;">Análisis de composición corporal</p>
      </td></tr>
      <tr><td style="padding:32px;">
        <p style="margin:0 0 16px 0;font-size:16px;color:#1f2937;line-height:1.5;">Hola <strong>{nombre}</strong>,</p>
        <p style="margin:0 0 16px 0;font-size:15px;color:#374151;line-height:1.6;">Gracias por sumarte al beta de SmartBioScan. Te escribo personalmente porque sos parte del primer grupo de nutricionistas que va a usar la herramienta antes del lanzamiento público.</p>
        <p style="margin:0 0 24px 0;font-size:15px;color:#374151;line-height:1.6;">Te creé una cuenta. Antes de empezar, necesito que establezcas tu contraseña:</p>
        <table role="presentation" cellpadding="0" cellspacing="0" style="margin:0 auto 16px auto;">
          <tr><td style="background-color:#0d7377;border-radius:6px;">
            <a href="{url}" style="display:inline-block;padding:14px 32px;font-size:15px;font-weight:600;color:#ffffff;text-decoration:none;">Establecer mi contraseña</a>
          </td></tr>
        </table>
        <p style="margin:0 0 32px 0;font-size:13px;color:#6b7280;text-align:center;">Este link es único y vence en 7 días.</p>
        <p style="margin:0 0 12px 0;font-size:15px;color:#374151;line-height:1.6;">Una vez adentro, vas a poder:</p>
        <ul style="margin:0 0 24px 0;padding-left:20px;font-size:15px;color:#374151;line-height:1.8;">
          <li>Cargar pacientes desde sus datos de Tanita</li>
          <li>Generar reportes de composición corporal en PDF, listos para entregar</li>
          <li>Ver la evolución de tus pacientes a lo largo del tiempo</li>
        </ul>
        <p style="margin:0 0 24px 0;font-size:15px;color:#374151;line-height:1.6;">Tu cuenta tiene créditos suficientes para que pruebes la herramienta con calma.</p>
        <div style="margin:0 0 24px 0;padding:16px 20px;background-color:#f9fafb;border-left:3px solid #0d7377;border-radius:4px;">
          <p style="margin:0 0 12px 0;font-size:14px;color:#374151;line-height:1.6;"><strong>Algunas cosas para tener en cuenta:</strong></p>
          <ul style="margin:0;padding-left:18px;font-size:14px;color:#4b5563;line-height:1.7;">
            <li>Estás usando una versión en desarrollo. Es posible que encuentres cosas que no funcionan como esperás. Cuando pase, escribime y lo arreglamos rápido.</li>
            <li>El envío automático por WhatsApp todavía no está disponible. Por ahora podés descargar el PDF y enviarlo manualmente.</li>
            <li>Lo que más necesito de vos es feedback honesto. Qué te resulta confuso, qué te falta, qué te molesta. Sin eso, esto no mejora.</li>
          </ul>
        </div>
        <p style="margin:0 0 16px 0;font-size:15px;color:#374151;line-height:1.6;">Cualquier duda, respondé este mismo correo. Lo leo yo.</p>
        <p style="margin:0 0 24px 0;font-size:15px;color:#374151;line-height:1.6;">Gracias por confiar.</p>
        <p style="margin:0;font-size:15px;color:#1f2937;line-height:1.5;"><strong>Lic. Diana Makk</strong><br><span style="color:#6b7280;font-size:14px;">SmartBioScan</span></p>
      </td></tr>
      <tr><td style="padding:20px 32px;background-color:#f9fafb;border-top:1px solid #eef0f3;border-radius:0 0 8px 8px;text-align:center;">
        <p style="margin:0;font-size:12px;color:#9ca3af;line-height:1.5;">Recibís este correo porque te registraste en el beta de SmartBioScan.</p>
      </td></tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""

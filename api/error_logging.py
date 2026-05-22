"""
Middleware FastAPI para captura persistente de errores 5xx.

Captura todas las excepciones no manejadas + respuestas con status >=500,
sanitiza el request body, y persiste en la tabla `backend_errors` de Supabase.

Cada error genera un request_id (UUID) que se devuelve al cliente en el
response y como header X-Request-ID, para correlación con logs.
"""
from __future__ import annotations

import json
import logging
import traceback
import uuid
from typing import Any, Callable, Optional

from fastapi import Request
from fastapi.responses import JSONResponse
from jose import jwt as _jose_jwt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

_logger = logging.getLogger(__name__)

# Campos en request bodies cuyos valores hay que ocultar (case-insensitive)
_SENSITIVE_KEYS = {
    "password", "pwd", "pass", "token", "access_token", "refresh_token",
    "secret", "api_key", "apikey", "authorization", "auth",
    "tanita_password", "encryption_key",
}

_MAX_BODY_LEN = 2000      # request_body, response_body
_MAX_STACK_LEN = 10000    # stack_trace


def _sanitize_body(raw_bytes: bytes) -> str:
    """
    Convierte body a string sanitizado. Si es JSON, oculta campos sensibles.
    Si no es JSON, lo trunca y se devuelve tal cual.
    """
    if not raw_bytes:
        return ""
    try:
        text = raw_bytes.decode("utf-8", errors="replace")
    except Exception:
        return f"<binary:{len(raw_bytes)} bytes>"

    # Intentar parsear JSON y sanitizar
    try:
        parsed = json.loads(text)
        sanitized = _redact(parsed)
        text = json.dumps(sanitized, ensure_ascii=False)
    except (json.JSONDecodeError, ValueError):
        pass  # No es JSON, lo dejamos tal cual

    return _truncate(text, _MAX_BODY_LEN)


def _redact(obj: Any) -> Any:
    """Recursivamente reemplaza valores de campos sensibles por '[REDACTED]'."""
    if isinstance(obj, dict):
        return {
            k: ("[REDACTED]" if k.lower() in _SENSITIVE_KEYS else _redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


def _truncate(s: Optional[str], max_len: int) -> Optional[str]:
    if s is None:
        return None
    return s if len(s) <= max_len else s[:max_len] + "...(truncated)"


def _extract_nutri_id_from_jwt(authorization: Optional[str]) -> Optional[str]:
    """
    Extrae el nutri_id (sub) del JWT SIN verificar firma.
    Es solo para logging — la auth real ya falló o no se evaluó si esto se
    está ejecutando como parte de un error handler.
    """
    if not authorization or not authorization.startswith("Bearer "):
        return None
    try:
        token = authorization.removeprefix("Bearer ")
        # decode sin verificar firma: solo queremos el sub para correlación
        payload = _jose_jwt.get_unverified_claims(token)
        return payload.get("sub")
    except Exception:
        return None


class BackendErrorLoggingMiddleware(BaseHTTPMiddleware):
    """
    Captura todo error >=500 (excepciones no manejadas o respuestas 5xx) y
    lo persiste en Supabase. Agrega header X-Request-ID al response.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        request_id = str(uuid.uuid4())
        # Disponible para handlers downstream si quieren usarlo
        request.state.request_id = request_id

        # Leer body antes de llamar el endpoint (necesitamos copiarlo)
        body_bytes = b""
        try:
            body_bytes = await request.body()

            # Re-inyectar el body para que el endpoint pueda leerlo
            async def receive() -> dict:
                return {"type": "http.request", "body": body_bytes, "more_body": False}
            request._receive = receive
        except Exception:
            pass  # Body no disponible (GET, stream, etc.)

        try:
            response = await call_next(request)
        except Exception as exc:
            # Excepción no manejada → 500
            self._log_error(
                request_id=request_id,
                request=request,
                body_bytes=body_bytes,
                status_code=500,
                error_message=str(exc) or type(exc).__name__,
                stack_trace=traceback.format_exc(),
                response_body=None,
            )
            response = JSONResponse(
                status_code=500,
                content={
                    "detail": "Internal server error",
                    "request_id": request_id,
                },
                headers={"X-Request-ID": request_id},
            )
            return response

        # Respuesta normal — chequear si es 5xx
        if response.status_code >= 500:
            # Capturar el body de respuesta (StreamingResponse aside)
            response_body = await self._read_response_body(response)
            self._log_error(
                request_id=request_id,
                request=request,
                body_bytes=body_bytes,
                status_code=response.status_code,
                error_message=f"HTTP {response.status_code}",
                stack_trace=None,
                response_body=response_body,
            )
            # Reconstruir response porque consumimos el body iterator
            response = Response(
                content=response_body or b"",
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=response.media_type,
            )

        response.headers["X-Request-ID"] = request_id
        return response

    @staticmethod
    async def _read_response_body(response: Response) -> bytes:
        """Lee el body de la respuesta para poder persistirlo."""
        if not hasattr(response, "body_iterator"):
            # Response normal — body ya está
            return getattr(response, "body", b"") or b""
        body = b""
        async for chunk in response.body_iterator:
            body += chunk
        return body

    @staticmethod
    def _log_error(
        request_id: str,
        request: Request,
        body_bytes: bytes,
        status_code: int,
        error_message: str,
        stack_trace: Optional[str],
        response_body: Optional[bytes],
    ) -> None:
        """Inserta el error en Supabase. Cualquier fallo al insertar se loguea pero no rompe el flow."""
        try:
            from db import DB
            db = DB()

            authorization = request.headers.get("authorization") or request.headers.get("Authorization")
            nutri_id = _extract_nutri_id_from_jwt(authorization)

            row = {
                "request_id":     request_id,
                "nutri_id":       nutri_id,
                "request_path":   _truncate(str(request.url.path), 500),
                "request_method": request.method,
                "status_code":    status_code,
                "error_message":  _truncate(error_message, _MAX_BODY_LEN),
                "stack_trace":    _truncate(stack_trace, _MAX_STACK_LEN),
                "request_body":   _sanitize_body(body_bytes),
                "response_body":  _sanitize_body(response_body or b""),
                "user_agent":     _truncate(request.headers.get("user-agent"), 500),
                "ip_address":     request.client.host if request.client else None,
            }

            db.client.table("backend_errors").insert(row).execute()

            _logger.error(
                "backend_error captured request_id=%s path=%s status=%d nutri=%s",
                request_id,
                row["request_path"],
                status_code,
                nutri_id or "anonymous",
            )
        except Exception as exc:
            # Crítico: no podemos perder visibility, pero tampoco loop infinito
            _logger.error(
                "FAILED to persist backend_error request_id=%s: %s",
                request_id, exc,
            )

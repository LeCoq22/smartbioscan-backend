"""
SmartBioScan — Receptor de mediciones nativas (balanza Tanita RD-545).

La app móvil (nativa_bioscan_mobile) captura el frame B010 de la balanza,
lo decodifica localmente y lo sube por su outbox local-first. Este módulo
recibe esas capturas, las valida contra un contrato estricto y las persiste
de forma idempotente en la tabla native_measurements.

Contrato de entrada: ver
  nativa-rd545-sdk/.../production/smartbioscan_sync.dart (NativeMeasurementUpload.toJson).

Registro de rutas (factory, igual que subscriptions.py / admin_regenerate.py):
    from api.native_measurements import register_routes
    register_routes(app, get_current_nutri)
"""

import json
import logging
import math
import re
import uuid
from datetime import datetime, date, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict

_logger = logging.getLogger("smartbioscan.native_measurements")


# ─────────────────────────────────────────────
# Allowlists y límites (exactos, según el contrato de la app)
# ─────────────────────────────────────────────

_SOURCE_ALLOWED         = "tanita_rd545"
_PARSER_VERSIONS_ALLOWED = {
    "b010-tanita-tags-v2",  # compatibilidad con capturas ya persistidas
    "b010-tanita-tags-v3",  # sign-magnitude correcto + PhysiqueRating derivado
}
_SEX_ALLOWED            = {"male", "female"}
_FIGURE_ALLOWED         = {"normal", "athlete"}
_ACTIVITY_ALLOWED       = {"sedentary", "moderate", "active", "athlete"}

_HEX_RE               = re.compile(r"^[0-9a-fA-F]+$")
_MAX_HEX_CHARS        = 8192
_MAX_DECODED_KEYS     = 200
_MAX_DECODED_BYTES    = 64 * 1024          # 64 KiB
_MAX_SCALE_LABEL      = 10
_MAX_SCALE_INSTALL_ID = 200
_MIN_CAPTURED_AT      = datetime(2015, 1, 1, tzinfo=timezone.utc)
_MIN_HEIGHT_CM        = 80.0
_MAX_HEIGHT_CM        = 250.0
_MIN_BIRTH_DATE       = date(1900, 1, 1)


# ─────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────

class _ProfileSnapshotIn(BaseModel):
    # extra="forbid": un campo no declarado (p.ej. un nutri_id inyectado) es
    # rechazado con 422 en vez de ignorarse silenciosamente.
    model_config = ConfigDict(extra="forbid")

    birth_date: str
    sex: str
    height_cm: float
    figure: str
    activity: str
    scale_label: str


class NativeMeasurementIn(BaseModel):
    # extra="forbid": el nutri_id se deriva EXCLUSIVAMENTE del JWT; cualquier
    # campo extra en el body (incluido un nutri_id) es rechazado con 422.
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str
    patient_id: str
    captured_at: str
    source: str
    scale_installation_id: str
    raw_b010_hex: str
    decoded_fields: dict
    parser_version: str
    profile_snapshot: _ProfileSnapshotIn


class NativeMeasurementResponse(BaseModel):
    ok: bool
    measurement_id: str
    created: bool


class NativeReportResponse(BaseModel):
    ok: bool
    report_id: str
    created: bool


class NativeMeasurementListItem(BaseModel):
    id: str
    patient_id: str
    captured_at: str
    decoded_fields: dict
    parser_version: str
    profile_snapshot: dict
    report_id: Optional[str] = None


class NativeMeasurementListResponse(BaseModel):
    items: list[NativeMeasurementListItem]
    limit: int
    offset: int


# ─────────────────────────────────────────────
# Helpers de validación
# ─────────────────────────────────────────────

def _reject(msg: str) -> HTTPException:
    return HTTPException(status_code=422, detail=msg)


def _normalize_uuid(raw: str, field: str) -> str:
    try:
        return str(uuid.UUID(str(raw)))
    except (ValueError, AttributeError, TypeError):
        raise _reject(f"{field} no es un UUID válido")


def _parse_captured_at(raw: str) -> datetime:
    """Parsea ISO-8601 (acepta sufijo 'Z') y valida el rango permitido."""
    if not isinstance(raw, str) or not raw.strip():
        raise _reject("captured_at requerido")
    s = raw.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise _reject("captured_at no es una fecha ISO-8601 válida")
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise _reject("captured_at debe incluir zona horaria explícita")
    dt = dt.astimezone(timezone.utc)

    now = datetime.now(timezone.utc)
    if dt < _MIN_CAPTURED_AT:
        raise _reject("captured_at anterior a 2015")
    if dt > now + timedelta(days=1):
        raise _reject("captured_at en el futuro")
    return dt


def _validate_raw_hex(raw: str) -> None:
    if not isinstance(raw, str) or not raw:
        raise _reject("raw_b010_hex vacío")
    if len(raw) > _MAX_HEX_CHARS:
        raise _reject(f"raw_b010_hex excede {_MAX_HEX_CHARS} caracteres")
    if len(raw) % 2 != 0:
        raise _reject("raw_b010_hex tiene longitud impar")
    if not _HEX_RE.match(raw):
        raise _reject("raw_b010_hex no es hexadecimal")


def _validate_decoded_fields(decoded: dict) -> None:
    if not isinstance(decoded, dict):
        raise _reject("decoded_fields debe ser un objeto")
    if len(decoded) > _MAX_DECODED_KEYS:
        raise _reject(f"decoded_fields excede {_MAX_DECODED_KEYS} claves")
    for k, v in decoded.items():
        if not isinstance(k, str):
            raise _reject("decoded_fields tiene una clave no textual")
        # bool es subclase de int en Python: excluir explícitamente.
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise _reject(f"decoded_fields['{k}'] no es numérico")
        if isinstance(v, float) and not math.isfinite(v):
            raise _reject(f"decoded_fields['{k}'] no es finito")
    try:
        size = len(json.dumps(decoded, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError):
        raise _reject("decoded_fields no es serializable")
    if size > _MAX_DECODED_BYTES:
        raise _reject("decoded_fields excede 64 KiB")


def _validate_profile_snapshot(snap: _ProfileSnapshotIn) -> dict:
    if snap.sex not in _SEX_ALLOWED:
        raise _reject("profile_snapshot.sex inválido")
    if snap.figure not in _FIGURE_ALLOWED:
        raise _reject("profile_snapshot.figure inválido")
    if snap.activity not in _ACTIVITY_ALLOWED:
        raise _reject("profile_snapshot.activity inválido")

    label = snap.scale_label
    if not (1 <= len(label) <= _MAX_SCALE_LABEL):
        raise _reject("profile_snapshot.scale_label debe tener 1..10 caracteres")
    if any(not (0x20 <= ord(c) <= 0x7E) for c in label):
        raise _reject("profile_snapshot.scale_label debe ser ASCII imprimible")

    try:
        birth = date.fromisoformat(snap.birth_date)
    except (ValueError, TypeError):
        raise _reject("profile_snapshot.birth_date debe ser YYYY-MM-DD")
    today = datetime.now(timezone.utc).date()
    if not (_MIN_BIRTH_DATE <= birth <= today):
        raise _reject("profile_snapshot.birth_date fuera de rango (1900..hoy)")

    if not math.isfinite(snap.height_cm) or not (_MIN_HEIGHT_CM <= snap.height_cm <= _MAX_HEIGHT_CM):
        raise _reject("profile_snapshot.height_cm fuera de rango")

    return {
        "birth_date":  snap.birth_date,
        "sex":         snap.sex,
        "height_cm":   snap.height_cm,
        "figure":      snap.figure,
        "activity":    snap.activity,
        "scale_label": label,
    }


def _is_unique_violation(exc: Exception) -> bool:
    """True si la excepción del cliente Postgres es una violación unique (23505)."""
    code = getattr(exc, "code", None)
    if code == "23505":
        return True
    details = getattr(exc, "details", "") or ""
    msg = f"{exc} {details}".lower()
    return "23505" in msg or "duplicate key" in msg or "unique constraint" in msg


# ─────────────────────────────────────────────
# Registro de rutas
# ─────────────────────────────────────────────

def register_routes(app, get_current_nutri_dep):
    """
    Registra el router en la app principal.
    Se llama desde api/main.py pasando la dependencia get_current_nutri.

    El router se crea acá (no a nivel de módulo) para que cada registro sea
    independiente: evita acumular rutas si register_routes se llama más de una
    vez (p. ej. en tests con distintas dependencias).
    """
    router = APIRouter(prefix="/api/native-measurements", tags=["native-measurements"])

    @router.get("", response_model=NativeMeasurementListResponse)
    async def list_native_measurements(
        patient_id: str,
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0),
        nutri_id: str = Depends(get_current_nutri_dep),
    ):
        """Lista el historial Nativa propio sin exponer B010 ni IDs ajenos."""
        patient_uuid = _normalize_uuid(patient_id, "patient_id")
        from db import DB
        db = DB()
        if not db.get_owned_patient_status(patient_uuid, nutri_id):
            raise HTTPException(status_code=404, detail="Paciente no encontrado")

        rows = db.list_owned_native_measurements(
            patient_uuid,
            nutri_id,
            limit=limit,
            offset=offset,
        )
        report_by_measurement = {
            str(row["native_measurement_id"]): str(row["id"])
            for row in db.get_reports_by_native_measurements(
                [str(row["id"]) for row in rows],
                nutri_id,
            )
        }
        return NativeMeasurementListResponse(
            items=[
                NativeMeasurementListItem(
                    id=str(row["id"]),
                    patient_id=str(row["patient_id"]),
                    captured_at=str(row["captured_at"]),
                    decoded_fields=row.get("decoded_fields") or {},
                    parser_version=str(row["parser_version"]),
                    profile_snapshot=row.get("profile_snapshot") or {},
                    report_id=report_by_measurement.get(str(row["id"])),
                )
                for row in rows
            ],
            limit=limit,
            offset=offset,
        )

    @router.post("", response_model=NativeMeasurementResponse)
    async def ingest_native_measurement(
        body: NativeMeasurementIn,
        nutri_id: str = Depends(get_current_nutri_dep),
        idempotency_key_header: Optional[str] = Header(None, alias="Idempotency-Key"),
    ):
        """
        Recibe una medición nativa de la balanza y la persiste de forma idempotente.

        - nutri_id se deriva EXCLUSIVAMENTE del JWT (get_current_nutri), nunca del body.
        - Idempotency-Key es obligatorio, UUID válido, y debe coincidir con
          body.idempotency_key.
        - El paciente debe pertenecer al nutri y estar activo.

        Respuesta 200: {ok, measurement_id, created}. No devuelve raw ni datos clínicos.
        """
        # ── 1. Idempotency-Key: obligatorio, UUID, coincide con el body ──
        if not idempotency_key_header:
            raise HTTPException(status_code=400, detail="Idempotency-Key header requerido")
        header_uuid = _normalize_uuid(idempotency_key_header, "Idempotency-Key")
        body_uuid   = _normalize_uuid(body.idempotency_key, "idempotency_key")
        if header_uuid != body_uuid:
            raise HTTPException(
                status_code=400,
                detail="Idempotency-Key no coincide con body.idempotency_key",
            )
        idem = header_uuid

        # ── 2. Allowlists de source / parser_version ──
        if body.source != _SOURCE_ALLOWED:
            raise _reject("source inválido")
        if body.parser_version not in _PARSER_VERSIONS_ALLOWED:
            raise _reject("parser_version inválido")

        # ── 3. patient_id UUID ──
        patient_uuid = _normalize_uuid(body.patient_id, "patient_id")

        # ── 4. scale_installation_id ──
        scale_install_id = (body.scale_installation_id or "").strip()
        if not scale_install_id:
            raise _reject("scale_installation_id requerido")
        if len(scale_install_id) > _MAX_SCALE_INSTALL_ID:
            raise _reject("scale_installation_id demasiado largo")

        # ── 5. hex / decoded_fields / captured_at / profile_snapshot ──
        _validate_raw_hex(body.raw_b010_hex)
        _validate_decoded_fields(body.decoded_fields)
        captured_at = _parse_captured_at(body.captured_at)
        profile_snapshot = _validate_profile_snapshot(body.profile_snapshot)

        # ── 6. Paciente propio y activo (service_role bypassa RLS → filtrar id+nutri) ──
        from db import DB
        db = DB()
        patient = db.get_owned_patient_status(patient_uuid, nutri_id)
        if not patient:
            # ajeno o inexistente — no distinguimos para no filtrar existencia
            raise HTTPException(status_code=404, detail="Paciente no encontrado")
        if not patient.get("is_active", False):
            raise HTTPException(status_code=409, detail="Paciente inactivo")

        # ── 7. Persistencia atómica: INSERT primero; en conflicto 23505, lookup ──
        row = {
            "nutri_id":              nutri_id,
            "patient_id":            patient_uuid,
            "idempotency_key":       idem,
            "captured_at":           captured_at.isoformat(),
            "source":                body.source,
            "scale_installation_id": scale_install_id,
            "raw_b010_hex":          body.raw_b010_hex,
            "decoded_fields":        body.decoded_fields,
            "parser_version":        body.parser_version,
            "profile_snapshot":      profile_snapshot,
        }

        try:
            inserted = db.insert_native_measurement(row)
        except Exception as exc:
            if _is_unique_violation(exc):
                existing = db.get_native_measurement_by_idempotency(nutri_id, idem)
                if existing:
                    return NativeMeasurementResponse(
                        ok=True,
                        measurement_id=str(existing["id"]),
                        created=False,
                    )
                # Conflicto unique sin fila recuperable: no debería pasar; loguear.
                _logger.error(
                    "native_measurement conflicto unique sin fila nutri=%s idem=%s",
                    nutri_id, idem,
                )
                raise HTTPException(status_code=500, detail="Conflicto de inserción no resuelto")
            _logger.error("native_measurement insert error nutri=%s: %s", nutri_id, exc)
            raise HTTPException(status_code=500, detail="No se pudo guardar la medición")

        return NativeMeasurementResponse(
            ok=True,
            measurement_id=str(inserted["id"]),
            created=True,
        )

    @router.post("/{measurement_id}/report", response_model=NativeReportResponse)
    async def generate_native_report(
        measurement_id: str,
        nutri_id: str = Depends(get_current_nutri_dep),
    ):
        measurement_uuid = _normalize_uuid(measurement_id, "measurement_id")
        from native_report_pipeline import run_native_report_pipeline
        result = await run_native_report_pipeline(measurement_uuid, nutri_id)
        return NativeReportResponse(
            ok=True,
            report_id=result["report_id"],
            created=not result.get("skipped", False),
        )

    app.include_router(router)

"""Adaptador de mediciones BLE Nativa al dominio de análisis SmartBioScan."""

from __future__ import annotations

import math
from datetime import datetime

from analysis_engine import TanitaMeasurement


class NativeMeasurementDataError(ValueError):
    """La captura nativa no contiene un dato clínico válido requerido."""


def decode_tanita_signed_magnitude(value: int) -> int | None:
    """Decodifica el byte sign-magnitude usado por los scores Tanita."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255:
        raise ValueError("Tanita score byte debe estar en 0..255")
    if value == 0xFF:
        return None
    magnitude = value & 0x7F
    return -magnitude if value & 0x80 else magnitude


def calculate_physique_rating(body_fat_judgement: int, muscle_mass_score: int) -> int:
    """Replica exactamente la matriz 3x3 de MyTanita (resultado 1..9)."""
    if not 0 <= body_fat_judgement <= 5 or not -4 <= muscle_mass_score <= 4:
        return 0
    fat_band = 0 if body_fat_judgement >= 4 else 1 if body_fat_judgement >= 2 else 2
    muscle_band = 0 if muscle_mass_score <= -2 else 1 if muscle_mass_score <= 1 else 2
    return fat_band * 3 + muscle_band + 1


def _number(fields: dict, key: str, *, required: bool = True, default: float = 0.0) -> float:
    value = fields.get(key)
    if value is None and not required:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NativeMeasurementDataError(f"decoded_fields.{key} requerido y numérico")
    value = float(value)
    if not math.isfinite(value):
        raise NativeMeasurementDataError(f"decoded_fields.{key} no es finito")
    return value


def _integer(fields: dict, key: str) -> int:
    value = _number(fields, key)
    if not value.is_integer():
        raise NativeMeasurementDataError(f"decoded_fields.{key} debe ser entero")
    return int(value)


def _muscle_mass_score(fields: dict, parser_version: str) -> int:
    score = _integer(fields, "MuscleMassScore")
    # v2 exponía el byte crudo como unsigned. v3 ya lo entrega decodificado.
    if parser_version == "b010-tanita-tags-v2" and 0x80 <= score <= 0xFF:
        decoded = decode_tanita_signed_magnitude(score)
        if decoded is None:
            raise NativeMeasurementDataError("MuscleMassScore no disponible")
        score = decoded
    return score


def native_row_to_tanita_measurement(row: dict) -> TanitaMeasurement:
    """Construye el mismo objeto que hoy produce el CSV, sin MyTanita."""
    fields = row.get("decoded_fields")
    if not isinstance(fields, dict):
        raise NativeMeasurementDataError("decoded_fields ausente")
    parser_version = str(row.get("parser_version") or "")

    fat_judgement = _integer(fields, "BodyFatJudgement")
    muscle_score = _muscle_mass_score(fields, parser_version)
    physique_rating = calculate_physique_rating(fat_judgement, muscle_score)
    if physique_rating == 0:
        raise NativeMeasurementDataError("judgements inválidos para Physique Rating")

    captured_at = str(row.get("captured_at") or "")
    try:
        datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise NativeMeasurementDataError("captured_at inválido") from exc

    return TanitaMeasurement(
        date=captured_at,
        weight_kg=_number(fields, "Weight"),
        bmi=_number(fields, "BodyMassIndex"),
        body_fat_pct=_number(fields, "BodyFat"),
        visceral_fat=_number(fields, "VisceralFat"),
        muscle_mass_kg=_number(fields, "MuscleMass"),
        muscle_quality=_number(fields, "MuscleQuality"),
        bone_mass_kg=_number(fields, "BoneMass"),
        bmr_kcal=_number(fields, "BasalMetabolicRate"),
        metabolic_age=_integer(fields, "MetabolicAge"),
        body_water_pct=_number(fields, "BodyWater"),
        physique_rating=physique_rating,
        muscle_right_arm=_number(fields, "MuscleMassRightArm"),
        muscle_left_arm=_number(fields, "MuscleMassLeftArm"),
        muscle_right_leg=_number(fields, "MuscleMassRightFoot"),
        muscle_left_leg=_number(fields, "MuscleMassLeftFoot"),
        muscle_trunk=_number(fields, "MuscleMassTrunk"),
        quality_right_arm=_number(fields, "MuscleQualityRightArm"),
        quality_left_arm=_number(fields, "MuscleQualityLeftArm"),
        quality_right_leg=_number(fields, "MuscleQualityRightFoot"),
        quality_left_leg=_number(fields, "MuscleQualityLeftFoot"),
        # La RD-545 no entrega calidad muscular del tronco en este B010.
        quality_trunk=0.0,
        fat_pct_right_arm=_number(fields, "BodyFatRightArm"),
        fat_pct_left_arm=_number(fields, "BodyFatLeftArm"),
        fat_pct_right_leg=_number(fields, "BodyFatRightFoot"),
        fat_pct_left_leg=_number(fields, "BodyFatLeftFoot"),
        fat_pct_trunk=_number(fields, "BodyFatTrunk"),
        heart_rate=_number(fields, "EpPulse", required=False) or None,
        source="app",
    )

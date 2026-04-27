"""
Parser del CSV generado por el scraper de MyTanita.
Convierte las filas del CSV en objetos TanitaMeasurement.
"""

import csv
from datetime import datetime
from pathlib import Path
from analysis_engine import TanitaMeasurement


def parse_float(value: str, default: float = 0.0) -> float:
    """Parsea float, devuelve default si es '-' o vacío."""
    try:
        return float(value.strip())
    except (ValueError, AttributeError):
        return default


def parse_int(value: str, default: int = 0) -> int:
    try:
        return int(float(value.strip()))
    except (ValueError, AttributeError):
        return default


def load_csv(filepath: str) -> list[TanitaMeasurement]:
    """
    Carga el CSV del scraper y devuelve lista de TanitaMeasurement ordenada por fecha.

    Columnas esperadas (en orden):
    Date, Weight (kg), BMI, Body Fat (%), Visc Fat, Muscle Mass (kg),
    Muscle Quality, Bone Mass (kg), BMR (kcal), Metab Age, Body Water (%),
    Physique Rating, Muscle mass - right arm, Muscle mass - left arm,
    Muscle mass - right leg, Muscle mass - left leg, Muscle mass - trunk,
    Muscle quality - right arm, Muscle quality - left arm,
    Muscle quality - right leg, Muscle quality - left leg, Muscle quality - trunk,
    Body fat (%) - right arm, Body fat (%) - left arm,
    Body fat (%) - right leg, Body fat (%) - left leg, Body fat (%) - trunk,
    Heart rate
    """
    measurements = []
    path = Path(filepath)

    with open(path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            m = TanitaMeasurement(
                date=row['Date'].strip().strip('"'),
                weight_kg=parse_float(row['Weight (kg)']),
                bmi=parse_float(row['BMI']),
                body_fat_pct=parse_float(row['Body Fat (%)']),
                visceral_fat=parse_float(row['Visc Fat']),
                muscle_mass_kg=parse_float(row['Muscle Mass (kg)']),
                muscle_quality=parse_float(row['Muscle Quality']),
                bone_mass_kg=parse_float(row['Bone Mass (kg)']),
                bmr_kcal=parse_float(row['BMR (kcal)']),
                metabolic_age=parse_int(row['Metab Age']),
                body_water_pct=parse_float(row['Body Water (%)']),
                physique_rating=parse_int(row['Physique Rating']),
                muscle_right_arm=parse_float(row['Muscle mass - right arm']),
                muscle_left_arm=parse_float(row['Muscle mass - left arm']),
                muscle_right_leg=parse_float(row['Muscle mass - right leg']),
                muscle_left_leg=parse_float(row['Muscle mass - left leg']),
                muscle_trunk=parse_float(row['Muscle mass - trunk']),
                quality_right_arm=parse_float(row['Muscle quality - right arm']),
                quality_left_arm=parse_float(row['Muscle quality - left arm']),
                quality_right_leg=parse_float(row['Muscle quality - right leg']),
                quality_left_leg=parse_float(row['Muscle quality - left leg']),
                quality_trunk=parse_float(row['Muscle quality - trunk']),
                fat_pct_right_arm=parse_float(row['Body fat (%) - right arm']),
                fat_pct_left_arm=parse_float(row['Body fat (%) - left arm']),
                fat_pct_right_leg=parse_float(row['Body fat (%) - right leg']),
                fat_pct_left_leg=parse_float(row['Body fat (%) - left leg']),
                fat_pct_trunk=parse_float(row['Body fat (%) - trunk']),
                heart_rate=parse_float(row.get('Heart rate', '0')) or None,
            )
            measurements.append(m)

    # Ordenar por fecha
    measurements.sort(key=lambda x: x.date)
    return measurements

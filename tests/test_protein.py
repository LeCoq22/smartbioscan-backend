from types import SimpleNamespace

import pytest

from analysis_engine import compute_protein


def measurement(**overrides):
    values = {
        "weight_kg": 66.75,
        "body_fat_pct": 33.4,
        "muscle_mass_kg": 42.25,
        "body_water_pct": 48.5,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_protein_matches_tanita_muscle_minus_body_water():
    result = compute_protein(measurement(), "F")

    assert result["protein_kg"] == 9.88
    assert result["protein_pct"] == 14.8
    assert result["kg_cat"] == "Alto"
    assert result["pct_cat"] == "Alto"


@pytest.mark.parametrize("body_water_pct", [0, -1])
def test_protein_falls_back_when_body_water_is_unavailable(body_water_pct):
    result = compute_protein(measurement(body_water_pct=body_water_pct), "F")

    assert result["protein_kg"] == 8.89
    assert result["protein_pct"] == 13.3


def test_protein_never_becomes_negative_for_inconsistent_measurements():
    result = compute_protein(
        measurement(weight_kg=100, muscle_mass_kg=20, body_water_pct=50),
        "F",
    )

    assert result["protein_kg"] == 0.0
    assert result["protein_pct"] == 0.0

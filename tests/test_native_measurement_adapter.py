import pytest

from native_measurement_adapter import (
    NativeMeasurementDataError,
    calculate_physique_rating,
    decode_tanita_signed_magnitude,
    native_row_to_tanita_measurement,
)


def _fields(**overrides):
    values = {
        "Weight": 80.25,
        "BodyMassIndex": 24.8,
        "BodyFat": 20.1,
        "VisceralFat": 8.0,
        "MuscleMass": 60.0,
        "MuscleMassScore": 0,
        "MuscleQuality": 72,
        "BoneMass": 3.1,
        "BasalMetabolicRate": 1800,
        "MetabolicAge": 40,
        "BodyWater": 55.2,
        "BodyFatJudgement": 5,
        "MuscleMassRightArm": 3.5,
        "MuscleMassLeftArm": 3.4,
        "MuscleMassRightFoot": 10.1,
        "MuscleMassLeftFoot": 10.0,
        "MuscleMassTrunk": 33.0,
        "MuscleQualityRightArm": 70,
        "MuscleQualityLeftArm": 69,
        "MuscleQualityRightFoot": 74,
        "MuscleQualityLeftFoot": 73,
        "BodyFatRightArm": 18.0,
        "BodyFatLeftArm": 18.2,
        "BodyFatRightFoot": 21.0,
        "BodyFatLeftFoot": 20.8,
        "BodyFatTrunk": 20.2,
        "EpPulse": 67,
    }
    values.update(overrides)
    return values


def test_signed_magnitude_is_not_twos_complement():
    assert decode_tanita_signed_magnitude(0x81) == -1
    assert decode_tanita_signed_magnitude(0x84) == -4
    assert decode_tanita_signed_magnitude(0x04) == 4
    assert decode_tanita_signed_magnitude(0xFF) is None


@pytest.mark.parametrize(
    ("fat", "muscle", "rating"),
    [(5, -4, 1), (5, 0, 2), (5, 4, 3),
     (3, -4, 4), (3, 0, 5), (3, 4, 6),
     (1, -4, 7), (1, 0, 8), (1, 4, 9)],
)
def test_physique_matrix(fat, muscle, rating):
    assert calculate_physique_rating(fat, muscle) == rating


def test_builds_app_measurement_and_recomputes_physique():
    measurement = native_row_to_tanita_measurement({
        "captured_at": "2026-07-16T10:30:00+00:00",
        "parser_version": "b010-tanita-tags-v3",
        "decoded_fields": _fields(PhysiqueRating=9),
    })
    assert measurement.physique_rating == 2  # no confía en el campo derivado
    assert measurement.source == "app"
    assert measurement.heart_rate == 67
    assert measurement.quality_trunk == 0


def test_normalizes_legacy_v2_unsigned_score():
    measurement = native_row_to_tanita_measurement({
        "captured_at": "2026-07-16T10:30:00Z",
        "parser_version": "b010-tanita-tags-v2",
        "decoded_fields": _fields(MuscleMassScore=0x81),
    })
    assert measurement.physique_rating == 2


def test_rejects_incomplete_clinical_payload():
    fields = _fields()
    del fields["Weight"]
    with pytest.raises(NativeMeasurementDataError, match="Weight"):
        native_row_to_tanita_measurement({
            "captured_at": "2026-07-16T10:30:00Z",
            "parser_version": "b010-tanita-tags-v3",
            "decoded_fields": fields,
        })

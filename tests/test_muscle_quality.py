import pytest

from analysis_engine import classify_mq_total


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (40, "Bajo"),
        (41, "Normal"),
        (48, "Normal"),
        (66, "Normal"),
        (67, "Alto"),
    ],
)
def test_global_muscle_quality_uses_female_50s_tanita_range(score, expected):
    assert classify_mq_total(score, "F", 55) == expected


@pytest.mark.parametrize(
    ("score", "expected"),
    [(38, "Bajo"), (39, "Normal"), (63, "Normal"), (64, "Alto")],
)
def test_global_muscle_quality_uses_male_50s_tanita_range(score, expected):
    assert classify_mq_total(score, "M", 55) == expected

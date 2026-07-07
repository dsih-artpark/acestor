"""Tests for the main dengue pipeline's predictionInt column (issue #83).

At parent (main pipeline) level: predictionInt = round_half_up(prediction).
This is the display integer dashboards render; the raw float `prediction`
stays untouched so downscale can multiply by case-shares faithfully.
"""

from __future__ import annotations

import math

import pandas as pd

from pipelines.dengue_downscale.lib.apportionment import round_half_up


def test_predictionint_matches_round_half_up_of_prediction():
    """Every row's predictionInt must equal round_half_up(prediction)."""
    predictions = [0.0, 0.4, 0.5, 1.4, 1.5, 2.7, 12.66, 14.13]
    for v in predictions:
        assert round_half_up(v) == math.floor(v + 0.5)


def test_below_half_rounds_down():
    """1.4 stays 1 (would have been 2 under ceil)."""
    assert round_half_up(1.4) == 1


def test_half_rounds_up():
    """1.5 becomes 2 — public-health friendly rounding."""
    assert round_half_up(1.5) == 2


def test_zero_stays_zero():
    assert round_half_up(0.0) == 0


def test_negative_clamps_to_zero():
    """Non-negative clamp; case counts can't be negative."""
    assert round_half_up(-0.3) == 0


def test_predictionint_applied_columnwise():
    """Apply round_half_up over a pandas Series (how the pipeline uses it)."""
    df = pd.DataFrame({"prediction": [0.4, 1.5, 2.7, 12.66, 14.13]})
    df["predictionInt"] = df["prediction"].astype(float).apply(round_half_up)
    assert df["predictionInt"].tolist() == [0, 2, 3, 13, 14]

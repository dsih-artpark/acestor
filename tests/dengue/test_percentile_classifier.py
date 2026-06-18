"""Tests for the percentile-based risk classifier (#62).

Covers:
  * ThresholdsConfig.from_raw allowlist validation (was silently coercing
    unknown values to WHO before the fix).
  * ThresholdsConfig.percentile_cutoffs parsing + default.
  * percentile_historical_zones — per-region historical-percentile band
    assignment, including the canonical [25, 50, 75] case, NaN handling,
    and the per-region semantics (a region with no history gets NA, not 0).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pipelines.dengue.configs import ThresholdsConfig
from pipelines.dengue.lib.thresholds import percentile_historical_zones


# ---------------------------------------------------------------------------
# Config — allowlist + percentile_cutoffs
# ---------------------------------------------------------------------------


def test_classification_method_allowlist_rejects_unknown():
    """Unknown values silently produced WHO output until 2026 — must now raise."""
    with pytest.raises(ValueError, match="classification_method"):
        ThresholdsConfig.from_raw({"classification_method": "typo"})


def test_classification_method_allowlist_accepts_who_icmr_percentile():
    for method in ("who", "icmr", "percentile"):
        cfg = ThresholdsConfig.from_raw({"classification_method": method})
        assert cfg.classification_method == method


def test_classification_method_default_is_who():
    cfg = ThresholdsConfig.from_raw({})
    assert cfg.classification_method == "who"


def test_percentile_cutoffs_defaults_to_25_50_75():
    cfg = ThresholdsConfig.from_raw({})
    assert cfg.percentile_cutoffs == [25.0, 50.0, 75.0]


def test_percentile_cutoffs_parses_list():
    cfg = ThresholdsConfig.from_raw(
        {"classification_method": "percentile", "percentile_cutoffs": [33, 66, 90]}
    )
    assert cfg.percentile_cutoffs == [33.0, 66.0, 90.0]


# ---------------------------------------------------------------------------
# percentile_historical_zones — the classifier itself
# ---------------------------------------------------------------------------


def test_percentile_zones_canonical_25_50_75():
    """4 bands from 3 cutoffs at the 25/50/75th percentiles of region history."""
    case_history = pd.DataFrame(
        {
            "region_id": ["r1"] * 100,
            "case": list(range(1, 101)),  # 1..100 — percentiles are 25.75, 50.5, 75.25
        }
    )
    preds = pd.DataFrame(
        {
            "region_id": ["r1", "r1", "r1", "r1"],
            "prediction": [10.0, 40.0, 60.0, 90.0],
        }
    )
    out = percentile_historical_zones(
        preds, case_history, spatial_col="region_id", percentile_cutoffs=[25, 50, 75]
    )
    zones = out["predictionZone"].tolist()
    # 10 < 25.75 → band 1; 40 in (25.75, 50.5) → band 2;
    # 60 in (50.5, 75.25) → band 3; 90 > 75.25 → band 4.
    assert zones == [1, 2, 3, 4]


def test_percentile_zones_per_region_semantics():
    """Same prediction value lands in different bands for different histories.

    This is the headline behavior — the band reflects where the prediction
    sits within the **region's own** history, not a global threshold.
    """
    case_history = pd.DataFrame(
        {
            "region_id": ["high"] * 20 + ["low"] * 20,
            "case": [200] * 20 + [2] * 20,
        }
    )
    preds = pd.DataFrame(
        {
            "region_id": ["high", "low"],
            "prediction": [50.0, 50.0],  # identical predictions
        }
    )
    out = percentile_historical_zones(
        preds, case_history, spatial_col="region_id", percentile_cutoffs=[50]
    )
    # high: prediction 50 < median 200 → band 1.
    # low: prediction 50 > median 2 → band 2.
    assert out.set_index("region_id")["predictionZone"]["high"] == 1
    assert out.set_index("region_id")["predictionZone"]["low"] == 2


def test_percentile_zones_no_history_returns_na():
    """A prediction for a region absent from case_data gets NA, not 0."""
    case_history = pd.DataFrame({"region_id": ["r1"] * 10, "case": list(range(10))})
    preds = pd.DataFrame({"region_id": ["unknown"], "prediction": [5.0]})
    out = percentile_historical_zones(
        preds, case_history, spatial_col="region_id", percentile_cutoffs=[50]
    )
    assert pd.isna(out["predictionZone"].iloc[0])


def test_percentile_zones_nan_prediction_returns_na():
    """A NaN prediction must yield NA, not silently land in band 1."""
    case_history = pd.DataFrame({"region_id": ["r1"] * 10, "case": list(range(10))})
    preds = pd.DataFrame({"region_id": ["r1"], "prediction": [float("nan")]})
    out = percentile_historical_zones(
        preds, case_history, spatial_col="region_id", percentile_cutoffs=[50]
    )
    assert pd.isna(out["predictionZone"].iloc[0])


def test_percentile_zones_single_cutoff_gives_two_bands():
    """N cutoffs → N+1 bands. With [50] the median splits low/high."""
    case_history = pd.DataFrame({"region_id": ["r1"] * 10, "case": list(range(10))})
    preds = pd.DataFrame({"region_id": ["r1", "r1"], "prediction": [1.0, 8.0]})
    out = percentile_historical_zones(
        preds, case_history, spatial_col="region_id", percentile_cutoffs=[50]
    )
    assert out["predictionZone"].tolist() == [1, 2]


def test_percentile_zones_unsorted_cutoffs_are_sorted_internally():
    """Cutoffs passed out of order must still produce the canonical band order."""
    case_history = pd.DataFrame({"region_id": ["r1"] * 100, "case": list(range(100))})
    preds = pd.DataFrame({"region_id": ["r1"], "prediction": [40.0]})
    a = percentile_historical_zones(
        preds, case_history, spatial_col="region_id", percentile_cutoffs=[25, 50, 75]
    )
    b = percentile_historical_zones(
        preds, case_history, spatial_col="region_id", percentile_cutoffs=[75, 25, 50]
    )
    assert a["predictionZone"].iloc[0] == b["predictionZone"].iloc[0]


def test_percentile_zones_multi_region_independent():
    """Per-region cutoffs are computed independently from each region's slice."""
    rng = np.random.default_rng(7)
    rows = []
    for region, scale in [("a", 1.0), ("b", 100.0)]:
        for v in rng.integers(0, 10, size=40):
            rows.append({"region_id": region, "case": float(v) * scale})
    case_history = pd.DataFrame(rows)
    # Each region gets its own cutoff at the 50th percentile of its own values.
    preds = pd.DataFrame(
        {
            "region_id": ["a", "a", "b", "b"],
            "prediction": [0.0, 100.0, 0.0, 1000.0],
        }
    )
    out = percentile_historical_zones(
        preds, case_history, spatial_col="region_id", percentile_cutoffs=[50]
    )
    # 0 ≤ median for both → band 1; large values → band 2.
    zones = out["predictionZone"].tolist()
    assert zones[0] == 1 and zones[2] == 1
    assert zones[1] == 2 and zones[3] == 2

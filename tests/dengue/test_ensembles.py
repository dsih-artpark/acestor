"""Tests for the ensemble strategy registry."""

import pandas as pd
import pytest

from pipelines.dengue.lib.ensembles import (
    BaseEnsemble,
    _ENSEMBLE_REGISTRY,
    get_ensemble,
)


def _two_pred_dfs():
    df_a = pd.DataFrame(
        {
            "region": ["r0", "r1"],
            "recordDate": pd.to_datetime(["2022-05-25", "2022-05-25"]),
            "prediction": [10.0, 20.0],
            "model": ["a", "a"],
        }
    )
    df_b = pd.DataFrame(
        {
            "region": ["r0", "r1"],
            "recordDate": pd.to_datetime(["2022-05-25", "2022-05-25"]),
            "prediction": [20.0, 40.0],
            "model": ["b", "b"],
        }
    )
    return [df_a, df_b]


def test_mean_in_registry():
    assert "mean" in _ENSEMBLE_REGISTRY


def test_mean_satisfies_protocol():
    assert isinstance(get_ensemble("mean"), BaseEnsemble)


def test_get_ensemble_unknown_raises():
    with pytest.raises(KeyError, match="weighted"):
        get_ensemble("weighted")


def test_mean_takes_arithmetic_mean():
    e = get_ensemble("mean")
    out = e.combine(_two_pred_dfs(), spatial_col="region")
    out = out.sort_values("region").reset_index(drop=True)
    assert out["prediction"].tolist() == [15.0, 30.0]
    assert (out["model"] == "ensembleModel").all()


def test_mean_collapses_divergent_isoweek_across_models():
    """Regression for issue #101 — models tag ISOWeek differently based on their
    own training tails (RF/XGB use last_4 recordDates; TSE uses a 14-day-earlier
    cutoff). If MeanEnsemble grouped by ISOWeek, this would produce TWO
    ensembleModel rows per target date, breaking downstream sanity checks.
    Both rows should collapse into one, averaging all three models."""
    target = pd.Timestamp("2026-07-27")  # ISO W31 Monday
    rf = pd.DataFrame(
        {
            "region": ["r0"],
            "recordDate": [target],
            "startDatePredictedWeek": [target.date().isoformat()],
            "thresholdMethod": ["historical"],
            "ISOWeek": [30],  # RF's training tail
            "prediction": [1.0],
            "model": ["rf"],
        }
    )
    xgb = pd.DataFrame(
        {
            "region": ["r0"],
            "recordDate": [target],
            "startDatePredictedWeek": [target.date().isoformat()],
            "thresholdMethod": ["historical"],
            "ISOWeek": [30],  # XGB agrees with RF
            "prediction": [1.2],
            "model": ["xgb"],
        }
    )
    tse = pd.DataFrame(
        {
            "region": ["r0"],
            "recordDate": [target],
            "startDatePredictedWeek": [target.date().isoformat()],
            "thresholdMethod": ["historical"],
            "ISOWeek": [29],  # TSE 14-day-earlier
            "prediction": [0.5],
            "model": ["tse"],
        }
    )

    out = get_ensemble("mean").combine([rf, xgb, tse], spatial_col="region")

    # Must be exactly one row — the divergent ISOWeek must NOT split the group.
    assert len(out) == 1
    row = out.iloc[0]
    # Mean of all three models, not just RF/XGB or just TSE.
    assert row["prediction"] == pytest.approx((1.0 + 1.2 + 0.5) / 3)
    # ISOWeek must reflect the target week's true ISO week (31), not any model's
    # stale tail-week metadata (29 or 30).
    assert int(row["ISOWeek"]) == 31
    assert row["model"] == "ensembleModel"

"""Unit tests for the recursive multi-week forecast helper (issue #44)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from pipelines.dengue.lib.models._shared import recursive_forecast

SPATIAL = "region"


class _Echo:
    """Stub model: returns case_lag_1 — echoes whatever last week's case is.

    Lets us prove the recursion feeds each week's prediction into the next
    week's case lag.
    """

    def __init__(self, feature_cols):
        self._i = feature_cols.index("case_lag_1")

    def predict(self, X):
        return np.array([float(row[self._i]) for row in X])


class _Const:
    def __init__(self, value):
        self.value = value

    def predict(self, X):
        return np.array([float(self.value)] * len(X))


def _df0(region, observed, future_dates, *, weather=1.0, start="2026-01-06"):
    """Build a ret_na_filled-style frame: observed cases then future weeks (case NaN)."""
    dates = list(pd.date_range(start, periods=len(observed), freq="7D"))
    rows = []
    for d, c in zip(dates, observed):
        rows.append({SPATIAL: region, "recordDate": d, "case": float(c),
                     "case_lag_1": np.nan, "t2m_lag_12": weather})
    for d in future_dates:
        rows.append({SPATIAL: region, "recordDate": pd.Timestamp(d), "case": np.nan,
                     "case_lag_1": np.nan, "t2m_lag_12": weather})
    return pd.DataFrame(rows)


def _future(start_after, n=4, start="2026-01-06"):
    last_obs = pd.date_range(start, periods=start_after, freq="7D")[-1]
    return list(pd.date_range(last_obs + pd.Timedelta(weeks=1), periods=n, freq="7D"))


def test_emits_all_four_horizons():
    fut = _future(8, n=4)
    df0 = _df0("r1", [3, 4, 5, 6, 7, 8, 9, 10], fut)
    preds, _ = recursive_forecast(
        _Const(5.0), df0, spatial_col=SPATIAL,
        feature_cols=["case_lag_1", "t2m_lag_12"], lag_cases=[1], future_dates=fut,
    )
    assert len(preds) == 4
    assert set(pd.to_datetime(preds["recordDate"])) == set(fut)


def test_recursion_feeds_prediction_forward():
    fut = _future(8, n=4)
    # cutoff (last observed) case = 10; Echo returns case_lag_1
    df0 = _df0("r1", [3, 4, 5, 6, 7, 8, 9, 10], fut)
    preds, debug = recursive_forecast(
        _Echo(["case_lag_1", "t2m_lag_12"]), df0, spatial_col=SPATIAL,
        feature_cols=["case_lag_1", "t2m_lag_12"], lag_cases=[1], future_dates=fut,
        debug=True,
    )
    # week1 echoes observed cutoff (10); each later week echoes the prior prediction → all 10
    assert preds["prediction"].tolist() == [10.0, 10.0, 10.0, 10.0]
    dbg = pd.DataFrame(debug).sort_values("recordDate").reset_index(drop=True)
    assert dbg.loc[0, "case_lag_1_source"] == "observed"   # week1 lag is the real cutoff
    assert dbg.loc[1, "case_lag_1_source"] == "predicted"  # week2 lag is week1's forecast


def test_zero_pad_before_series_start():
    # only 1 observed week; lag 2 from the first future week reaches before start → 0
    fut = _future(1, n=1)
    df0 = _df0("r1", [10], fut)
    df0["case_lag_2"] = np.nan
    preds, debug = recursive_forecast(
        _Const(1.0), df0, spatial_col=SPATIAL,
        feature_cols=["case_lag_2", "t2m_lag_12"], lag_cases=[2], future_dates=fut,
        debug=True,
    )
    assert debug[0]["case_lag_2_source"] == "zero_pad"
    assert debug[0]["case_lag_2_value"] == 0.0


def test_clip_off_by_default():
    fut = _future(8, n=1)
    df0 = _df0("r1", list(range(3, 11)), fut)
    preds, _ = recursive_forecast(
        _Const(1000.0), df0, spatial_col=SPATIAL,
        feature_cols=["case_lag_1", "t2m_lag_12"], lag_cases=[1], future_dates=fut,
        train_max=10.0,  # no clip_multiplier → no upper clip
    )
    assert preds["prediction"].iloc[0] == 1000.0
    assert not preds["was_clipped"].iloc[0]


def test_clip_applied_when_multiplier_set():
    fut = _future(8, n=1)
    df0 = _df0("r1", list(range(3, 11)), fut)
    preds, _ = recursive_forecast(
        _Const(1000.0), df0, spatial_col=SPATIAL,
        feature_cols=["case_lag_1", "t2m_lag_12"], lag_cases=[1], future_dates=fut,
        clip_multiplier=3.0, train_max=10.0,
    )
    assert preds["prediction"].iloc[0] == 30.0  # 3 * 10
    assert preds["was_clipped"].iloc[0]


def test_skips_region_with_nan_weather():
    fut = _future(8, n=4)
    df0 = _df0("r1", list(range(3, 11)), fut, weather=np.nan)  # weather lag NaN
    preds, _ = recursive_forecast(
        _Const(5.0), df0, spatial_col=SPATIAL,
        feature_cols=["case_lag_1", "t2m_lag_12"], lag_cases=[1], future_dates=fut,
    )
    assert preds.empty


def test_predictions_floored_at_zero():
    fut = _future(8, n=1)
    df0 = _df0("r1", list(range(3, 11)), fut)
    preds, _ = recursive_forecast(
        _Const(-5.0), df0, spatial_col=SPATIAL,
        feature_cols=["case_lag_1", "t2m_lag_12"], lag_cases=[1], future_dates=fut,
    )
    assert preds["prediction"].iloc[0] == 0.0

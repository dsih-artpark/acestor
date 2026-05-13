"""Tests for pipelines.dengue.steps.generate_thresholds — PRISM-H §4.4 fallback."""

import pandas as pd

from pipelines.dengue.steps.generate_thresholds import _apply_rolling_fallback


def _make_df(region_ids, dates, means, stds, method="historical"):
    return pd.DataFrame(
        {
            "region_id": region_ids,
            "date": pd.to_datetime(dates),
            "case": [0] * len(region_ids),
            "Mean": means,
            "StdDev": stds,
            "threshold_method": [method] * len(region_ids),
        }
    )


# ---------------------------------------------------------------------------
# _apply_rolling_fallback
# ---------------------------------------------------------------------------


def test_historical_mean_zero_replaced_by_prev_nweeks():
    """Historical Mean=0 for a region+date is replaced by prev_nweeks Mean for same region+date."""
    date = "2024-01-08"

    historical_df = _make_df(
        region_ids=["R_zero", "R_nonzero"],
        dates=[date, date],
        means=[0.0, 3.0],
        stds=[0.0, 1.0],
        method="historical",
    )
    rolling_df = _make_df(
        region_ids=["R_zero", "R_nonzero"],
        dates=[date, date],
        means=[5.0, 7.0],
        stds=[2.0, 2.5],
        method="previousNweeks",
    )

    result = _apply_rolling_fallback(historical_df, rolling_df, spatial_col="region_id")

    # R_zero had Mean=0 — must be replaced with prev_nweeks Mean=5.0
    r_zero = result[result["region_id"] == "R_zero"].iloc[0]
    assert r_zero["Mean"] == 5.0, f"Expected Mean=5.0 (fallback), got {r_zero['Mean']}"
    assert (
        r_zero["StdDev"] == 2.0
    ), f"Expected StdDev=2.0 (fallback), got {r_zero['StdDev']}"

    # R_nonzero had Mean=3.0 — must be preserved
    r_nonzero = result[result["region_id"] == "R_nonzero"].iloc[0]
    assert (
        r_nonzero["Mean"] == 3.0
    ), f"Expected Mean=3.0 (preserved), got {r_nonzero['Mean']}"
    assert (
        r_nonzero["StdDev"] == 1.0
    ), f"Expected StdDev=1.0 (preserved), got {r_nonzero['StdDev']}"


def test_fallback_not_applied_when_rolling_mean_is_zero():
    """If prev_nweeks Mean is also 0, the historical row must NOT be replaced."""
    date = "2024-01-08"
    historical_df = _make_df(["R1"], [date], [0.0], [0.0])
    rolling_df = _make_df(["R1"], [date], [0.0], [0.0], method="previousNweeks")

    result = _apply_rolling_fallback(historical_df, rolling_df, spatial_col="region_id")
    assert result.iloc[0]["Mean"] == 0.0


def test_fallback_not_applied_when_rolling_mean_is_nan():
    """If prev_nweeks Mean is NaN, the historical row is not replaced."""

    date = "2024-01-08"
    historical_df = _make_df(["R1"], [date], [0.0], [0.0])
    rolling_df = _make_df(
        ["R1"], [date], [float("nan")], [float("nan")], method="previousNweeks"
    )

    result = _apply_rolling_fallback(historical_df, rolling_df, spatial_col="region_id")
    assert result.iloc[0]["Mean"] == 0.0


def test_fallback_no_match_in_rolling_preserves_historical():
    """If no prev_nweeks row exists for a (region, date) pair, historical row is unchanged."""
    date = "2024-01-08"
    other_date = "2024-01-15"
    historical_df = _make_df(["R1"], [date], [0.0], [0.0])
    rolling_df = _make_df(["R1"], [other_date], [5.0], [2.0], method="previousNweeks")

    result = _apply_rolling_fallback(historical_df, rolling_df, spatial_col="region_id")
    assert result.iloc[0]["Mean"] == 0.0


def test_fallback_preserves_other_columns():
    """Fallback only changes Mean and StdDev — threshold_method and case are preserved."""
    date = "2024-01-08"
    historical_df = _make_df(["R1"], [date], [0.0], [0.0])
    rolling_df = _make_df(["R1"], [date], [5.0], [2.0], method="previousNweeks")

    result = _apply_rolling_fallback(historical_df, rolling_df, spatial_col="region_id")
    assert result.iloc[0]["threshold_method"] == "historical"
    assert result.iloc[0]["case"] == 0


def test_fallback_does_not_mutate_input():
    """_apply_rolling_fallback must return a copy — input historical_df is unchanged."""
    date = "2024-01-08"
    historical_df = _make_df(["R1"], [date], [0.0], [0.0])
    rolling_df = _make_df(["R1"], [date], [5.0], [2.0], method="previousNweeks")

    original_mean = historical_df.iloc[0]["Mean"]
    _apply_rolling_fallback(historical_df, rolling_df, spatial_col="region_id")
    assert historical_df.iloc[0]["Mean"] == original_mean, "Input DataFrame was mutated"


def test_fallback_no_zeros_returns_unchanged():
    """When no historical Mean=0 rows exist, output equals input."""
    date = "2024-01-08"
    historical_df = _make_df(["R1", "R2"], [date, date], [3.0, 4.0], [1.0, 1.5])
    rolling_df = _make_df(
        ["R1", "R2"], [date, date], [5.0, 6.0], [2.0, 2.5], method="previousNweeks"
    )

    result = _apply_rolling_fallback(historical_df, rolling_df, spatial_col="region_id")
    pd.testing.assert_frame_equal(result, historical_df)


def test_fallback_multiple_dates_correct_lookup():
    """Fallback matches by (region, date) — different dates must not bleed into each other."""
    dates = ["2024-01-08", "2024-01-15"]
    historical_df = _make_df(["R1", "R1"], dates, [0.0, 2.0], [0.0, 1.0])
    # prev_nweeks has different means on different dates
    rolling_df = _make_df(
        ["R1", "R1"], dates, [7.0, 8.0], [3.0, 3.5], method="previousNweeks"
    )

    result = _apply_rolling_fallback(historical_df, rolling_df, spatial_col="region_id")

    # First date (Mean was 0) → replaced with 7.0
    r1_first = result[result["date"] == pd.Timestamp("2024-01-08")].iloc[0]
    assert r1_first["Mean"] == 7.0

    # Second date (Mean was 2.0) → preserved
    r1_second = result[result["date"] == pd.Timestamp("2024-01-15")].iloc[0]
    assert r1_second["Mean"] == 2.0

"""Tests for pipelines.dengue.lib.weather."""

import pandas as pd
import pytest

from pipelines.dengue.lib.weather import (
    normalise_columns,
    aggregate_daily,
    rolling_aggregate,
    sample_data,
    rename_columns_for_output,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hourly_df(region="r1", n_hours=72, t2m=20.0, tp=0.1):
    """Create a minimal sub-daily weather DataFrame (3 days of hourly data)."""
    dates = pd.date_range("2024-01-01", periods=n_hours, freq="h")
    return pd.DataFrame(
        {
            "region_id": [region] * n_hours,
            "date": dates,
            "2mTemperature": [t2m] * n_hours,
            "totalPrecipitation": [tp] * n_hours,
        }
    )


def _daily_df(region="r1", n_days=14, temp=20.0, precip=1.0):
    dates = pd.date_range("2024-01-01", periods=n_days, freq="D")
    return pd.DataFrame(
        {
            "region_id": [region] * n_days,
            "date": dates,
            "2mTemperature": [temp] * n_days,
            "totalPrecipitation": [precip] * n_days,
        }
    )


# ---------------------------------------------------------------------------
# normalise_columns
# ---------------------------------------------------------------------------


def test_normalise_columns_renames_t2m():
    df = pd.DataFrame({"t2m": [1.0], "d2m": [2.0], "tp": [0.1]})
    result = normalise_columns(df)
    assert "2mTemperature" in result.columns
    assert "2mDewpointTemperature" in result.columns
    assert "totalPrecipitation" in result.columns


def test_normalise_columns_renames_time():
    df = pd.DataFrame({"time": ["2024-01-01"], "t2m": [1.0]})
    result = normalise_columns(df)
    assert "date" in result.columns


def test_normalise_columns_passthrough_unknown():
    df = pd.DataFrame({"unknown_col": [1.0]})
    result = normalise_columns(df)
    assert "unknown_col" in result.columns


def test_normalise_columns_no_rename_needed():
    df = pd.DataFrame({"2mTemperature": [20.0]})
    result = normalise_columns(df)
    assert list(result.columns) == ["2mTemperature"]


# ---------------------------------------------------------------------------
# aggregate_daily
# ---------------------------------------------------------------------------


def test_aggregate_daily_reduces_to_one_row_per_region_per_day():
    # 72 hours → IST shift spreads into 3 calendar days; first/last trimmed → interior days
    df = _hourly_df(n_hours=72)
    result = aggregate_daily(df, weather_vars=["2mTemperature", "totalPrecipitation"])
    # Each remaining day has exactly one row per region
    assert result.groupby(["region_id", "date"]).size().max() == 1


def test_aggregate_daily_temperature_is_mean():
    df = _hourly_df(n_hours=72, t2m=25.0)
    result = aggregate_daily(df, weather_vars=["2mTemperature"])
    assert abs(result["2mTemperature"].iloc[0] - 25.0) < 0.01


def test_aggregate_daily_precipitation_is_sum():
    df = _hourly_df(n_hours=72, tp=1.0)
    result = aggregate_daily(df, weather_vars=["totalPrecipitation"])
    # Interior days have exactly 24 hours × 1.0 = 24.0 after IST offset trimming
    assert abs(result["totalPrecipitation"].iloc[0] - 24.0) < 0.01


def test_aggregate_daily_no_matching_vars_raises():
    df = pd.DataFrame(
        {
            "region_id": ["r1"],
            "date": ["2024-01-01"],
            "someOtherCol": [1.0],
        }
    )
    with pytest.raises(ValueError, match="none of weather_vars"):
        aggregate_daily(df, weather_vars=["2mTemperature"])


def test_aggregate_daily_custom_rule_with_output_name():
    df = _hourly_df(n_hours=72, t2m=30.0)
    rules = [{"name": "2mTemperature", "op": "max", "output_name": "2mTemperature_max"}]
    result = aggregate_daily(df, weather_vars=["2mTemperature"], daily_agg=rules)
    assert "2mTemperature_max" in result.columns


# ---------------------------------------------------------------------------
# rolling_aggregate
# ---------------------------------------------------------------------------


def test_rolling_aggregate_output_has_same_rows():
    df = _daily_df(n_days=14)
    result = rolling_aggregate(
        df, weather_vars=["2mTemperature", "totalPrecipitation"], n_days=7
    )
    assert len(result) == len(df)


def test_rolling_aggregate_precip_is_sum():
    df = _daily_df(n_days=14, precip=1.0)
    result = rolling_aggregate(df, weather_vars=["totalPrecipitation"], n_days=7)
    # Last row: 7-day window of 1.0 each = 7.0
    last = result.sort_values("date").iloc[-1]
    assert abs(last["totalPrecipitation"] - 7.0) < 0.01


def test_rolling_aggregate_temp_is_mean():
    df = _daily_df(n_days=14, temp=20.0)
    result = rolling_aggregate(df, weather_vars=["2mTemperature"], n_days=7)
    last = result.sort_values("date").iloc[-1]
    assert abs(last["2mTemperature"] - 20.0) < 0.01


def test_rolling_aggregate_multiple_regions():
    df1 = _daily_df(region="r1", n_days=7)
    df2 = _daily_df(region="r2", n_days=7)
    df = pd.concat([df1, df2], ignore_index=True)
    result = rolling_aggregate(df, weather_vars=["2mTemperature"], n_days=7)
    assert set(result["region_id"].unique()) == {"r1", "r2"}


# ---------------------------------------------------------------------------
# sample_data
# ---------------------------------------------------------------------------


def test_weather_sample_data_from_end():
    df = _daily_df(n_days=14)
    result = sample_data(df, sampling_rate=7)
    assert len(result) == 2
    # dates[::-7] on sorted 14 dates → selects index 13 ("2024-01-14") and index 6 ("2024-01-07")
    result_dates = sorted(str(pd.Timestamp(d).date()) for d in result["date"].unique())
    assert result_dates == ["2024-01-07", "2024-01-14"]


def test_weather_sample_data_with_end_date():
    df = _daily_df(n_days=21)
    result = sample_data(df, end_date="2024-01-14", sampling_rate=7)
    assert all(pd.Timestamp(d) <= pd.Timestamp("2024-01-14") for d in result["date"])


def test_weather_sample_data_from_beginning():
    df = _daily_df(n_days=14)
    result = sample_data(df, sample_from="beginning", sampling_rate=7)
    assert len(result) == 2
    # dates[::7] on sorted 14 dates → selects index 0 ("2024-01-01") and index 7 ("2024-01-08")
    result_dates = sorted(str(pd.Timestamp(d).date()) for d in result["date"].unique())
    assert result_dates == ["2024-01-01", "2024-01-08"]


# ---------------------------------------------------------------------------
# rename_columns_for_output
# ---------------------------------------------------------------------------


def test_weather_rename_columns_district():
    df = _daily_df()
    result = rename_columns_for_output(df, "district")
    assert "location.admin2.ID" in result.columns
    assert "metadata.primaryDate" in result.columns


def test_weather_rename_columns_zone():
    df = _daily_df()
    result = rename_columns_for_output(df, "zone")
    assert "location.admin3.ID" in result.columns


def test_weather_rename_columns_preserves_weather_vars():
    df = _daily_df()
    result = rename_columns_for_output(df, "district")
    assert "2mTemperature" in result.columns
    assert "totalPrecipitation" in result.columns

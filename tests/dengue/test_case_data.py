"""Tests for pipelines.dengue.lib.case_data."""

import pandas as pd
import pytest

from pipelines.dengue.lib.case_data import (
    aggregate_standardized_data_daily,
    rolling_aggregate,
    sample_data,
    rename_columns_for_output,
    get_latest_sampling_day,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_linelist(dates, region_ids):
    return pd.DataFrame(
        {
            "metadata.primaryDate": dates,
            "location.admin2.ID": region_ids,
        }
    )


# ---------------------------------------------------------------------------
# aggregate_standardized_data_daily
# ---------------------------------------------------------------------------


def test_aggregate_standardized_basic():
    df = _make_linelist(
        ["2024-01-01", "2024-01-01", "2024-01-02"],
        ["d1", "d1", "d1"],
    )
    result = aggregate_standardized_data_daily([df], region_type="district")
    row = result[
        (result["region_id"] == "d1") & (result["date"] == pd.Timestamp("2024-01-01"))
    ]
    assert row["case"].values[0] == 2


def test_aggregate_standardized_fills_missing_dates():
    df = _make_linelist(
        ["2024-01-01", "2024-01-03"],
        ["d1", "d1"],
    )
    result = aggregate_standardized_data_daily([df], region_type="district")
    row = result[
        (result["region_id"] == "d1") & (result["date"] == pd.Timestamp("2024-01-02"))
    ]
    assert row["case"].values[0] == 0


def test_aggregate_standardized_multiple_regions():
    df = _make_linelist(
        ["2024-01-01", "2024-01-01"],
        ["d1", "d2"],
    )
    result = aggregate_standardized_data_daily([df], region_type="district")
    assert set(result["region_id"].unique()) == {"d1", "d2"}


def test_aggregate_standardized_date_filter():
    df = _make_linelist(
        ["2024-01-01", "2024-01-05", "2024-01-10"],
        ["d1", "d1", "d1"],
    )
    result = aggregate_standardized_data_daily(
        [df],
        region_type="district",
        date_start=pd.Timestamp("2024-01-04"),
        date_end=pd.Timestamp("2024-01-06"),
    )
    dates = set(result["date"].dt.date.astype(str).tolist())
    assert "2024-01-01" not in dates
    assert "2024-01-10" not in dates
    # Only 2024-01-05 falls within the filter; gap-filling uses the region's own
    # date range (min→max), so no extra dates are synthesised.
    assert "2024-01-05" in dates


def test_aggregate_standardized_unknown_region_type_raises():
    df = _make_linelist(["2024-01-01"], ["d1"])
    with pytest.raises(ValueError, match="Unknown region_type"):
        aggregate_standardized_data_daily([df], region_type="galaxy")


def test_aggregate_standardized_no_valid_dfs_raises():
    df = pd.DataFrame({"wrong_col": ["2024-01-01"]})
    with pytest.raises(ValueError, match="None of the provided"):
        aggregate_standardized_data_daily([df], region_type="district")


def test_aggregate_standardized_multiple_dfs():
    df1 = _make_linelist(["2024-01-01"], ["d1"])
    df2 = _make_linelist(["2024-01-02"], ["d1"])
    result = aggregate_standardized_data_daily([df1, df2], region_type="district")
    assert len(result) == 2


def test_aggregate_standardized_strips_trailing_z():
    df = pd.DataFrame(
        {
            "metadata.primaryDate": ["2024-01-01T00:00:00Z"],
            "location.admin2.ID": ["d1"],
        }
    )
    result = aggregate_standardized_data_daily([df], region_type="district")
    assert len(result) == 1


def test_aggregate_standardized_output_columns():
    df = _make_linelist(["2024-01-01"], ["d1"])
    result = aggregate_standardized_data_daily([df], region_type="district")
    assert set(result.columns) == {"region_id", "date", "case"}


# ---------------------------------------------------------------------------
# rolling_aggregate
# ---------------------------------------------------------------------------


def test_rolling_aggregate_7day_sum():
    dates = pd.date_range("2024-01-01", periods=7)
    df = pd.DataFrame(
        {
            "region_id": ["d1"] * 7,
            "date": dates,
            "case": [1] * 7,
        }
    )
    result = rolling_aggregate(df, n_days=7)
    last = result[result["date"] == dates[-1]]
    assert last["case"].values[0] == 7


def test_rolling_aggregate_preserves_regions():
    dates = pd.date_range("2024-01-01", periods=3)
    df = pd.DataFrame(
        {
            "region_id": ["d1", "d1", "d1", "d2", "d2", "d2"],
            "date": list(dates) * 2,
            "case": [1] * 6,
        }
    )
    result = rolling_aggregate(df, n_days=3)
    assert set(result["region_id"].unique()) == {"d1", "d2"}


# ---------------------------------------------------------------------------
# sample_data
# ---------------------------------------------------------------------------


def test_sample_data_from_end():
    dates = pd.date_range("2024-01-01", periods=14)
    df = pd.DataFrame(
        {
            "region_id": ["d1"] * 14,
            "date": dates,
            "case": range(14),
        }
    )
    result = sample_data(df, sampling_rate=7)
    assert len(result) == 2
    # dates[::-7] on sorted 14 dates → selects index 13 ("2024-01-14") and index 6 ("2024-01-07")
    result_dates = sorted(str(pd.Timestamp(d).date()) for d in result["date"].unique())
    assert result_dates == ["2024-01-07", "2024-01-14"]


def test_sample_data_with_end_date():
    dates = pd.date_range("2024-01-01", periods=21)
    df = pd.DataFrame(
        {
            "region_id": ["d1"] * 21,
            "date": dates,
            "case": range(21),
        }
    )
    result = sample_data(df, end_date="2024-01-14", sampling_rate=7)
    assert all(pd.Timestamp(d) <= pd.Timestamp("2024-01-14") for d in result["date"])


def test_sample_data_from_beginning():
    dates = pd.date_range("2024-01-01", periods=14)
    df = pd.DataFrame(
        {
            "region_id": ["d1"] * 14,
            "date": dates,
            "case": range(14),
        }
    )
    result = sample_data(df, sample_from="beginning", sampling_rate=7)
    assert len(result) == 2
    # dates[::7] on sorted 14 dates → selects index 0 ("2024-01-01") and index 7 ("2024-01-08")
    result_dates = sorted(str(pd.Timestamp(d).date()) for d in result["date"].unique())
    assert result_dates == ["2024-01-01", "2024-01-08"]


# ---------------------------------------------------------------------------
# rename_columns_for_output
# ---------------------------------------------------------------------------


def test_rename_columns_district():
    df = pd.DataFrame(
        {
            "region_id": ["d1"],
            "date": [pd.Timestamp("2024-01-01")],
            "case": [5],
        }
    )
    result = rename_columns_for_output(df, "district")
    assert "location.admin2.ID" in result.columns
    assert "metadata.primaryDate" in result.columns
    assert "case" in result.columns


def test_rename_columns_zone():
    df = pd.DataFrame(
        {
            "region_id": ["z1"],
            "date": [pd.Timestamp("2024-01-01")],
            "case": [3],
        }
    )
    result = rename_columns_for_output(df, "zone")
    assert "location.admin3.ID" in result.columns


# ---------------------------------------------------------------------------
# get_latest_sampling_day
# ---------------------------------------------------------------------------


def test_get_latest_sampling_day_wednesday():
    # 2024-01-10 is a Wednesday
    result = get_latest_sampling_day(pd.Timestamp("2024-01-12"), "W-WED")
    assert result == "2024-01-10"


def test_get_latest_sampling_day_same_day():
    # If given day is the sampling day itself
    result = get_latest_sampling_day(pd.Timestamp("2024-01-10"), "W-WED")
    assert result == "2024-01-10"

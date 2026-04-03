"""Tests for pipelines.dengue.lib.thresholds."""

import pandas as pd

from pipelines.dengue.lib.thresholds import (
    align_dates_all_regions,
    prev_nweeks_threshold_params,
    historical_threshold_params,
    combine_thresholds,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _case_df(region_ids, dates, cases):
    return pd.DataFrame(
        {
            "region_id": region_ids,
            "date": pd.to_datetime(dates),
            "case": cases,
        }
    )


# ---------------------------------------------------------------------------
# align_dates_all_regions
# ---------------------------------------------------------------------------


def test_align_dates_fills_gaps():
    df = _case_df(
        ["r1", "r1"],
        ["2024-01-01", "2024-01-03"],
        [2, 4],
    )
    result = align_dates_all_regions(df)
    gap = result[
        (result["region_id"] == "r1") & (result["date"] == pd.Timestamp("2024-01-02"))
    ]
    assert len(gap) == 1
    assert gap["case"].values[0] == 0


def test_align_dates_multiple_regions():
    df = _case_df(
        ["r1", "r1", "r2", "r2"],
        ["2024-01-01", "2024-01-03", "2024-01-01", "2024-01-03"],
        [1, 1, 2, 2],
    )
    result = align_dates_all_regions(df)
    assert set(result["region_id"].unique()) == {"r1", "r2"}
    assert len(result) == 6  # 3 dates x 2 regions


def test_align_dates_no_gaps():
    df = _case_df(
        ["r1", "r1", "r1"],
        ["2024-01-01", "2024-01-02", "2024-01-03"],
        [1, 2, 3],
    )
    result = align_dates_all_regions(df)
    assert len(result) == 3


def test_align_dates_output_columns():
    df = _case_df(["r1", "r1"], ["2024-01-01", "2024-01-03"], [1, 1])
    result = align_dates_all_regions(df)
    assert set(result.columns) == {"region_id", "date", "case"}


# ---------------------------------------------------------------------------
# prev_nweeks_threshold_params
# ---------------------------------------------------------------------------


def _weekly_df(n_weeks=8, region="r1", base_cases=2):
    dates = pd.date_range("2024-01-01", periods=n_weeks, freq="7D")
    return _case_df([region] * n_weeks, dates, [base_cases] * n_weeks)


def test_prev_nweeks_output_columns():
    df = _weekly_df()
    result = prev_nweeks_threshold_params(df, n=4, k=7)
    for col in ["region_id", "date", "case", "Mean", "StdDev", "threshold_method"]:
        assert col in result.columns


def test_prev_nweeks_threshold_method_label():
    df = _weekly_df()
    result = prev_nweeks_threshold_params(df, n=4, k=7)
    assert (result["threshold_method"] == "previousNweeks").all()


def test_prev_nweeks_constant_series_zero_std():
    df = _weekly_df(n_weeks=12, base_cases=5)
    result = prev_nweeks_threshold_params(df, n=4, k=7)
    non_nan = result["StdDev"].dropna()
    assert (non_nan == 0).all()
    non_nan_mean = result["Mean"].dropna()
    assert (non_nan_mean == 5.0).all()


def test_prev_nweeks_multiple_regions():
    df1 = _weekly_df(region="r1", base_cases=2)
    df2 = _weekly_df(region="r2", base_cases=4)
    df = pd.concat([df1, df2], ignore_index=True)
    result = prev_nweeks_threshold_params(df, n=4, k=7)
    assert set(result["region_id"].unique()) == {"r1", "r2"}
    # Each region should have independently computed means
    r1_mean = result[result["region_id"] == "r1"]["Mean"].dropna()
    r2_mean = result[result["region_id"] == "r2"]["Mean"].dropna()
    assert (r1_mean == 2.0).all()
    assert (r2_mean == 4.0).all()


# ---------------------------------------------------------------------------
# historical_threshold_params
# ---------------------------------------------------------------------------


def _multi_year_df():
    rows = []
    for year in [2022, 2023, 2024]:
        dates = pd.date_range(f"{year}-01-01", periods=52, freq="7D")
        for d in dates:
            rows.append({"region_id": "r1", "date": d, "case": 3})
    return pd.DataFrame(rows)


def test_historical_output_columns():
    df = _multi_year_df()
    result = historical_threshold_params(df)
    for col in ["region_id", "date", "case", "Mean", "StdDev", "threshold_method"]:
        assert col in result.columns


def test_historical_threshold_method_label():
    df = _multi_year_df()
    result = historical_threshold_params(df)
    assert (result["threshold_method"] == "historical").all()


def test_historical_first_year_has_nan_mean():
    df = _multi_year_df()
    result = historical_threshold_params(df)
    first_year = result[result["date"].dt.year == 2022]
    assert first_year["Mean"].isna().all()


def test_historical_excluded_years():
    df = _multi_year_df()
    result = historical_threshold_params(df, excluded_years=[2022])
    # 2023 should still have NaN (2022 excluded, nothing left before 2023)
    yr2023 = result[result["date"].dt.year == 2023]
    assert yr2023["Mean"].isna().all()


def test_historical_n_years_limits_lookback():
    df = _multi_year_df()
    result = historical_threshold_params(df, n_years=1)
    # 2024 should only use 2023 data — mean should still be 3.0
    yr2024 = result[(result["date"].dt.year == 2024) & result["Mean"].notna()]
    assert (yr2024["Mean"] == 3.0).all()


def test_historical_included_years():
    df = _multi_year_df()
    result = historical_threshold_params(df, included_years=[2023])
    # For year 2022: cond = (year < 2022) & (year in [2023]) → no rows → NaN
    yr2022 = result[result["date"].dt.year == 2022]
    assert yr2022["Mean"].isna().all()
    # For year 2023: cond = (year < 2023) & (year in [2023]) → no rows → NaN
    yr2023 = result[result["date"].dt.year == 2023]
    assert yr2023["Mean"].isna().all()
    # For year 2024: cond = (year < 2024) & (year in [2023]) → 2023 rows → mean = 3.0
    yr2024 = result[(result["date"].dt.year == 2024) & result["Mean"].notna()]
    assert (yr2024["Mean"] == 3.0).all()


# ---------------------------------------------------------------------------
# combine_thresholds
# ---------------------------------------------------------------------------


def test_combine_thresholds_concatenates():
    df1 = pd.DataFrame(
        {
            "region_id": ["r1"],
            "date": [pd.Timestamp("2024-01-01")],
            "Mean": [1.0],
            "StdDev": [0.5],
            "threshold_method": ["previousNweeks"],
        }
    )
    df2 = pd.DataFrame(
        {
            "region_id": ["r1"],
            "date": [pd.Timestamp("2024-01-01")],
            "Mean": [2.0],
            "StdDev": [1.0],
            "threshold_method": ["historical"],
        }
    )
    result = combine_thresholds([df1, df2])
    assert len(result) == 2
    assert set(result["threshold_method"].unique()) == {"previousNweeks", "historical"}


def test_combine_thresholds_sorted():
    df1 = pd.DataFrame(
        {
            "region_id": ["r1"],
            "date": [pd.Timestamp("2024-01-08")],
            "Mean": [1.0],
            "StdDev": [0.5],
            "threshold_method": ["previousNweeks"],
        }
    )
    df2 = pd.DataFrame(
        {
            "region_id": ["r1"],
            "date": [pd.Timestamp("2024-01-01")],
            "Mean": [2.0],
            "StdDev": [1.0],
            "threshold_method": ["historical"],
        }
    )
    result = combine_thresholds([df1, df2])
    dates = result["date"].tolist()
    assert dates == sorted(dates)

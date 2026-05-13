"""Tests for pipelines.dengue.lib.thresholds."""

import pytest
import pandas as pd

from pipelines.dengue.lib.thresholds import (
    ThresholdContext,
    _THRESHOLD_REGISTRY,
    align_dates_all_regions,
    combine_thresholds,
    get_threshold_method,
    historical_threshold_params,
    icmr_quartile_zones,
    prev_nweeks_threshold_params,
    register,
    weighted_baseline_threshold_params,
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


def test_prev_nweeks_constant_series_std_inflated():
    # Constant cases → raw σ=0, Mean=5.0 → inflation gives StdDev=sqrt(5.0) (PRISM-H §4.4)
    import numpy as np

    df = _weekly_df(n_weeks=12, base_cases=5)
    result = prev_nweeks_threshold_params(df, n=4, k=7)
    non_nan_mean = result["Mean"].dropna()
    assert (non_nan_mean == 5.0).all()
    non_nan_std = result["StdDev"].dropna()
    expected_std = np.sqrt(5.0)
    assert np.allclose(
        non_nan_std, expected_std, atol=1e-9
    ), f"Expected StdDev={expected_std} (inflated), got {non_nan_std.unique()}"


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


def test_registry_get_unknown_raises():
    with pytest.raises(KeyError, match="Unknown threshold method 'nonexistent'"):
        get_threshold_method("nonexistent")


def test_register_decorator_adds_to_registry():
    @register("_test_method")
    def _dummy(df, cfg):
        return df

    assert "_test_method" in _THRESHOLD_REGISTRY
    # cleanup
    del _THRESHOLD_REGISTRY["_test_method"]


def test_get_threshold_method_returns_callable():
    fn = get_threshold_method("historical")
    assert callable(fn)


def test_prev_nweeks_registered():
    fn = get_threshold_method("prev_nweeks")
    df = _weekly_df(n_weeks=12)
    aligned = align_dates_all_regions(df)
    ctx = ThresholdContext(n_weeks=4)
    result = fn(aligned, ctx)
    assert "threshold_method" in result.columns
    assert (result["threshold_method"] == "previousNweeks").all()


# ---------------------------------------------------------------------------
# icmr_quartile_zones
# ---------------------------------------------------------------------------


def _pred_df(region_ids, predictions, date="2024-01-01"):
    return pd.DataFrame(
        {
            "region_id": region_ids,
            "startDatePredictedWeek": pd.to_datetime(date),
            "prediction": predictions,
        }
    )


def test_icmr_quartile_all_zero_is_a4():
    df = _pred_df(["r1", "r2", "r3"], [0, 0, 0])
    result = icmr_quartile_zones(df)
    assert (result["predictionZone"] == 1).all()


def test_icmr_quartile_four_distinct_one_each():
    # 4 distinct values → 1 per stratum: top→4, next→3, next→2, last→1
    df = _pred_df(["r1", "r2", "r3", "r4"], [100, 75, 50, 25])
    result = icmr_quartile_zones(df).sort_values("prediction", ascending=False)
    assert list(result["predictionZone"]) == [4, 3, 2, 1]


def test_icmr_quartile_worked_example():
    # Worked example from PRISM-H doc §5.3: 26 districts, values_per_stratum=7
    # We'll use 8 distinct values → ceil(8/4)=2 per stratum
    preds = [100, 90, 80, 70, 60, 50, 40, 30]
    df = _pred_df([f"r{i}" for i in range(8)], preds)
    result = icmr_quartile_zones(df).sort_values("prediction", ascending=False)
    zones = list(result["predictionZone"])
    # top 2 → A1(4), next 2 → A2(3), next 2 → A3(2), last 2 → A4(1)
    assert zones == [4, 4, 3, 3, 2, 2, 1, 1]


def test_icmr_quartile_duplicates_collapse():
    # Three regions, two distinct values (100 and 50)
    # 2 distinct → ceil(2/4)=1 per stratum → 100→4, 50→3
    df = _pred_df(["r1", "r2", "r3"], [100, 100, 50])
    result = icmr_quartile_zones(df)
    high_zones = result.loc[result["prediction"] == 100, "predictionZone"].unique()
    assert list(high_zones) == [4]
    low_zones = result.loc[result["prediction"] == 50, "predictionZone"].unique()
    assert list(low_zones) == [3]


def test_icmr_quartile_per_date_independent():
    # Two dates with different distributions — classifications must be independent
    df = pd.DataFrame(
        {
            "region_id": ["r1", "r2", "r1", "r2"],
            "startDatePredictedWeek": pd.to_datetime(
                ["2024-01-01", "2024-01-01", "2024-01-08", "2024-01-08"]
            ),
            "prediction": [100, 50, 10, 5],
        }
    )
    result = icmr_quartile_zones(df)
    # On both dates: 2 distinct values → higher is A1(4), lower is A3(2)?
    # ceil(2/4)=1 per stratum: top→4, next→3 (3rd and 4th strata empty)
    for date in df["startDatePredictedWeek"].unique():
        sub = result[result["startDatePredictedWeek"] == date].sort_values(
            "prediction", ascending=False
        )
        assert list(sub["predictionZone"]) == [4, 3]


# ---------------------------------------------------------------------------
# weighted_baseline_threshold_params
# ---------------------------------------------------------------------------


def _wb_df(n_weeks=16, region="r1", base_cases=10):
    """Weekly df going back n_weeks — enough for both recent and seasonal windows."""
    dates = pd.date_range("2023-01-02", periods=n_weeks, freq="7D")
    return _case_df([region] * n_weeks, dates, [base_cases] * n_weeks)


def test_weighted_baseline_output_columns():
    df = _wb_df(n_weeks=60)
    result = weighted_baseline_threshold_params(df)
    for col in ["region_id", "date", "case", "Mean", "StdDev", "threshold_method"]:
        assert col in result.columns


def test_weighted_baseline_threshold_method_label():
    df = _wb_df(n_weeks=60)
    result = weighted_baseline_threshold_params(df)
    assert (result["threshold_method"] == "weightedBaseline").all()


def test_weighted_baseline_constant_series():
    # All cases = 10. Recent mean = 10, seasonal mean = 10. Weighted mean = 10.
    # SD over 8 weeks of constant data = 0 → inflated to sqrt(10) (PRISM-H §4.4).
    import numpy as np

    df = _wb_df(n_weeks=60, base_cases=10)
    result = weighted_baseline_threshold_params(df)
    non_nan = result["Mean"].dropna()
    assert (non_nan == 10.0).all()
    non_nan_sd = result["StdDev"].dropna()
    expected_std = np.sqrt(10.0)
    assert np.allclose(
        non_nan_sd, expected_std, atol=1e-9
    ), f"Expected StdDev={expected_std} (inflated from 0), got {non_nan_sd.unique()}"


def test_weighted_baseline_no_seasonal_falls_back_to_recent():
    # Only 8 weeks of data — no seasonal (52 weeks prior) available.
    # Mean should equal recent mean only.
    df = _wb_df(n_weeks=8, base_cases=5)
    result = weighted_baseline_threshold_params(df, recent_weeks=4)
    non_nan = result["Mean"].dropna()
    assert (non_nan == 5.0).all()


def test_weighted_baseline_multiple_regions_independent():
    df1 = _wb_df(n_weeks=60, region="r1", base_cases=4)
    df2 = _wb_df(n_weeks=60, region="r2", base_cases=20)
    df = pd.concat([df1, df2], ignore_index=True)
    result = weighted_baseline_threshold_params(df)
    r1_mean = result[result["region_id"] == "r1"]["Mean"].dropna()
    r2_mean = result[result["region_id"] == "r2"]["Mean"].dropna()
    assert (r1_mean == 4.0).all()
    assert (r2_mean == 20.0).all()


def test_weighted_baseline_registered():
    fn = get_threshold_method("weighted_baseline")
    df = _wb_df(n_weeks=60)
    aligned = align_dates_all_regions(df)
    ctx = ThresholdContext()
    result = fn(aligned, ctx)
    assert "threshold_method" in result.columns
    assert (result["threshold_method"] == "weightedBaseline").all()


def test_historical_registered():
    fn = get_threshold_method("historical")
    df = _multi_year_df()
    aligned = align_dates_all_regions(df)
    ctx = ThresholdContext(
        historical_n_years=None, excluded_years=[], included_years=[]
    )
    result = fn(aligned, ctx)
    assert "threshold_method" in result.columns
    assert (result["threshold_method"] == "historical").all()


def test_prev_nweeks_nu_excludes_current_week():
    # ν should be mean of weeks t-1..t-4, NOT including the current week.
    # Weeks 1-4: cases=10. Weeks 5-8: cases=99.
    # For week 5 (first 99-case week), ν must use only the four 10-case prior weeks.
    # With the corrected formula (skipna=False, full n-value windows required),
    # Mean may be NaN at this point (warm-up not complete), but must never exceed 10.0.
    dates = pd.date_range("2024-01-01", periods=8, freq="7D")
    cases = [10, 10, 10, 10, 99, 99, 99, 99]
    df = align_dates_all_regions(_case_df(["r1"] * 8, dates, cases))
    result = prev_nweeks_threshold_params(df, n=4, k=7)
    row = result[result["date"] == pd.Timestamp("2024-01-29")]
    assert not row.empty, "expected a row for 2024-01-29"
    mean_val = row["Mean"].values[0]
    # Mean is NaN (warm-up) or ≤ 10.0 — must never be > 10.0 (which would indicate
    # the current week's case=99 leaked into ν.
    import math

    assert (
        math.isnan(mean_val) or mean_val <= 10.0
    ), f"Mean={mean_val} — current week's case=99 must not contaminate ν"


def test_sigma_inflation_when_std_zero_mean_positive():
    """When StdDev=0 and Mean>0, StdDev must be inflated to sqrt(Mean) (PRISM-H §4.4)."""
    import numpy as np

    # Build a region with constant case counts — ν will be constant → σ=0
    n_weeks = 20
    dates = pd.date_range("2020-01-06", periods=n_weeks, freq="7D")
    df = pd.DataFrame(
        {
            "region_id": "R1",
            "date": dates,
            "case": 4.0,  # constant → every ν = 4.0 → σ = 0, Mean = 4.0
        }
    )
    result = prev_nweeks_threshold_params(df, n=4, k=7)
    valid = result.dropna(subset=["Mean", "StdDev"])
    # Mean should be 4.0 (constant)
    assert (
        valid["Mean"] == 4.0
    ).all(), f"Expected Mean=4.0, got {valid['Mean'].unique()}"
    # StdDev should be sqrt(4.0) = 2.0 (inflated from 0)
    expected_std = np.sqrt(4.0)
    assert np.allclose(
        valid["StdDev"], expected_std, atol=1e-9
    ), f"Expected StdDev={expected_std} after inflation, got {valid['StdDev'].unique()}"


def test_prev_nweeks_sigma_uses_4_nu_values():
    # σ must be std over 4 ν values, not 3.
    # Strictly increasing cases 1..10 over 10 weeks with k=7, n=4:
    #   ν at week 5 = mean(4,3,2,1) = 2.5
    #   ν at week 6 = mean(5,4,3,2) = 3.5
    #   ν at week 7 = mean(6,5,4,3) = 4.5
    #   ν at week 8 = mean(7,6,5,4) = 5.5
    # At week 8, σ = std([5.5, 4.5, 3.5, 2.5], ddof=1) ≈ 1.2910
    # If only 3 ν values were used: std([5.5, 4.5, 3.5], ddof=1) = 1.0 — clearly different.
    import numpy as np

    dates = pd.date_range("2020-01-06", periods=10, freq="7D")
    cases = list(range(1, 11))  # [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    df = align_dates_all_regions(_case_df(["r1"] * 10, dates, cases))
    result = prev_nweeks_threshold_params(df, n=4, k=7)

    # Week 8 is the first week that has all 4 ν values available for σ
    target_date = pd.Timestamp("2020-02-24")  # 2020-01-06 + 7*7 days
    row = result[result["date"] == target_date]
    assert not row.empty, f"expected a row for {target_date}"

    expected_4_value_std = np.std([5.5, 4.5, 3.5, 2.5], ddof=1)  # ≈ 1.2910
    wrong_3_value_std = np.std([5.5, 4.5, 3.5], ddof=1)  # = 1.0

    actual_std = row["StdDev"].values[0]
    assert abs(actual_std - expected_4_value_std) < 1e-6, (
        f"StdDev={actual_std:.6f} does not match 4-value std={expected_4_value_std:.6f}. "
        f"If σ used only 3 ν values it would be {wrong_3_value_std:.6f}."
    )
    # Explicitly confirm the 3-value answer is different (documents discriminating power)
    assert (
        abs(actual_std - wrong_3_value_std) > 0.1
    ), "Test is not discriminating: 3-value and 4-value std are too close"

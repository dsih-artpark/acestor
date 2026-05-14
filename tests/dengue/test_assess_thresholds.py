"""Tests for PRISM-H §4.2 method-selection logic in assess_thresholds."""

from __future__ import annotations

import pandas as pd

from pipelines.dengue.lib.thresholds import _select_best_method


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_final_row(
    date: str,
    method: str,
    row_count_total: int = 3,
    risk_zone_sum_total: float = 10.0,
    mean: float | None = 5.0,
) -> dict:
    """Build one row matching the shape of the `final` df in assess_thresholds."""
    return {
        "region_type": "District",
        "model": "nbr",
        "thresholdMethod": method,
        "Risk_Zone_Sum": risk_zone_sum_total,
        "Row_Count": row_count_total,
        "Risk_Zone_Sum_Total": risk_zone_sum_total,
        "Row_Count_Total": row_count_total,
        "Mean": float("nan") if mean is None else mean,
        "dateOfComputingPrediction": pd.Timestamp("2024-01-01"),
        "startDatePredictedWeek": pd.Timestamp(date),
        "Total_Region_Count": row_count_total,
        "Region_Count_Max": 10,
        "RatioCount.MethodVsTotal": 1.0,
    }


# ---------------------------------------------------------------------------
# PRISM-H §4.2 — method selection priority
# ---------------------------------------------------------------------------


def test_historical_preferred_over_prev_nweeks():
    """historical is selected when it has rows, even if prev_nweeks has higher Risk_Zone_Sum_Total."""
    rows = [
        _make_final_row(
            "2024-01-01", "historical", row_count_total=3, risk_zone_sum_total=5.0
        ),
        _make_final_row(
            "2024-01-01", "previousNweeks", row_count_total=3, risk_zone_sum_total=20.0
        ),
    ]
    final = pd.DataFrame(rows)
    best = _select_best_method(final)
    assert len(best) == 1
    assert (
        best.iloc[0]["thresholdMethod"] == "historical"
    ), f"Expected historical, got {best.iloc[0]['thresholdMethod']}"


def test_prev_nweeks_selected_when_historical_has_no_rows():
    """prev_nweeks is selected when historical has Mean=0 (no valid data)."""
    rows = [
        _make_final_row(
            "2024-01-01",
            "historical",
            row_count_total=0,
            risk_zone_sum_total=0.0,
            mean=0.0,
        ),
        _make_final_row(
            "2024-01-01",
            "previousNweeks",
            row_count_total=3,
            risk_zone_sum_total=7.0,
            mean=5.0,
        ),
    ]
    final = pd.DataFrame(rows)
    best = _select_best_method(final)
    assert len(best) == 1
    assert (
        best.iloc[0]["thresholdMethod"] == "previousNweeks"
    ), f"Expected previousNweeks, got {best.iloc[0]['thresholdMethod']}"


def test_prev_nweeks_selected_when_only_method():
    """prev_nweeks is selected when it is the only method available."""
    rows = [
        _make_final_row(
            "2024-01-01", "previousNweeks", row_count_total=5, risk_zone_sum_total=12.0
        ),
    ]
    final = pd.DataFrame(rows)
    best = _select_best_method(final)
    assert len(best) == 1
    assert best.iloc[0]["thresholdMethod"] == "previousNweeks"


def test_weighted_baseline_is_last_priority():
    """weighted_baseline is selected only when historical and prev_nweeks both have Mean=0."""
    rows = [
        _make_final_row(
            "2024-01-01",
            "historical",
            row_count_total=0,
            risk_zone_sum_total=0.0,
            mean=0.0,
        ),
        _make_final_row(
            "2024-01-01",
            "previousNweeks",
            row_count_total=0,
            risk_zone_sum_total=0.0,
            mean=0.0,
        ),
        _make_final_row(
            "2024-01-01",
            "weightedBaseline",
            row_count_total=4,
            risk_zone_sum_total=8.0,
            mean=3.0,
        ),
    ]
    final = pd.DataFrame(rows)
    best = _select_best_method(final)
    assert len(best) == 1
    assert (
        best.iloc[0]["thresholdMethod"] == "weightedBaseline"
    ), f"Expected weightedBaseline as fallback, got {best.iloc[0]['thresholdMethod']}"


def test_selection_per_date_independent():
    """Method selection is independent per date."""
    rows = [
        # date1: historical has rows → pick historical
        _make_final_row(
            "2024-01-01", "historical", row_count_total=3, risk_zone_sum_total=5.0
        ),
        _make_final_row(
            "2024-01-01", "previousNweeks", row_count_total=3, risk_zone_sum_total=20.0
        ),
        # date2: historical has Mean=0 (no valid data) → pick prev_nweeks
        _make_final_row(
            "2024-01-08",
            "historical",
            row_count_total=0,
            risk_zone_sum_total=0.0,
            mean=0.0,
        ),
        _make_final_row(
            "2024-01-08",
            "previousNweeks",
            row_count_total=3,
            risk_zone_sum_total=7.0,
            mean=5.0,
        ),
    ]
    final = pd.DataFrame(rows)
    best = _select_best_method(final)
    assert len(best) == 2

    d1 = best[best["startDatePredictedWeek"] == pd.Timestamp("2024-01-01")]
    d2 = best[best["startDatePredictedWeek"] == pd.Timestamp("2024-01-08")]

    assert (
        d1.iloc[0]["thresholdMethod"] == "historical"
    ), f"date1: expected historical, got {d1.iloc[0]['thresholdMethod']}"
    assert (
        d2.iloc[0]["thresholdMethod"] == "previousNweeks"
    ), f"date2: expected previousNweeks, got {d2.iloc[0]['thresholdMethod']}"


def test_all_methods_zero_rows_falls_back_to_any():
    """When all methods have Mean=0, returns first available (no crash)."""
    rows = [
        _make_final_row(
            "2024-01-01",
            "historical",
            row_count_total=0,
            risk_zone_sum_total=0.0,
            mean=0.0,
        ),
        _make_final_row(
            "2024-01-01",
            "previousNweeks",
            row_count_total=0,
            risk_zone_sum_total=0.0,
            mean=0.0,
        ),
    ]
    final = pd.DataFrame(rows)
    best = _select_best_method(final)
    # Should not raise, should return exactly one row
    assert len(best) == 1


def test_prev_nweeks_fallback_when_historical_null():
    """PRISM-H §4.2: historical with Mean=NaN is skipped in favour of prev_nweeks."""
    rows = [
        _make_final_row(
            "2024-01-01",
            "historical",
            row_count_total=3,
            risk_zone_sum_total=5.0,
            mean=None,
        ),
        _make_final_row(
            "2024-01-01",
            "previousNweeks",
            row_count_total=3,
            risk_zone_sum_total=7.0,
            mean=4.0,
        ),
    ]
    final = pd.DataFrame(rows)
    best = _select_best_method(final)
    assert len(best) == 1
    assert (
        best.iloc[0]["thresholdMethod"] == "previousNweeks"
    ), f"Expected previousNweeks (historical Mean=NaN), got {best.iloc[0]['thresholdMethod']}"


def test_historical_chosen_over_prev_nweeks_despite_lower_row_count():
    """PRISM-H §4.2: historical wins when Mean is valid, even with lower Row_Count_Total."""
    rows = [
        _make_final_row(
            "2024-01-01",
            "historical",
            row_count_total=2,
            risk_zone_sum_total=4.0,
            mean=5.0,
        ),
        _make_final_row(
            "2024-01-01",
            "previousNweeks",
            row_count_total=10,
            risk_zone_sum_total=30.0,
            mean=10.0,
        ),
    ]
    final = pd.DataFrame(rows)
    best = _select_best_method(final)
    assert len(best) == 1
    assert (
        best.iloc[0]["thresholdMethod"] == "historical"
    ), f"Expected historical (non-null Mean wins priority), got {best.iloc[0]['thresholdMethod']}"

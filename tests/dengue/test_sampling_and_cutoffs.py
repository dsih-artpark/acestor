"""Tests for pipelines.dengue.lib.sampling and cutoffs."""

import pandas as pd
import pytest

from pipelines.dengue.lib.sampling import get_day_abbreviation
from pipelines.dengue.lib.cutoffs import (
    estimate_cutoff_date,
    compute_prediction_dates,
    identify_cutoff_dates,
)


# ---------------------------------------------------------------------------
# get_day_abbreviation
# ---------------------------------------------------------------------------


def test_get_day_abbreviation_wednesday():
    # 2024-01-03 is a Wednesday
    assert get_day_abbreviation("2024-01-03") == "W-WED"


def test_get_day_abbreviation_monday():
    # 2024-01-01 is a Monday
    assert get_day_abbreviation("2024-01-01") == "W-MON"


def test_get_day_abbreviation_timestamp():
    result = get_day_abbreviation(pd.Timestamp("2024-01-05"))  # Friday
    assert result == "W-FRI"


def test_get_day_abbreviation_sunday():
    result = get_day_abbreviation("2024-01-07")  # Sunday
    assert result == "W-SUN"


# ---------------------------------------------------------------------------
# estimate_cutoff_date
# ---------------------------------------------------------------------------


def _make_region_df(dates, regions):
    return pd.DataFrame(
        {
            "recordDate": pd.to_datetime(dates),
            "region_id": regions,
        }
    )


def test_estimate_cutoff_returns_most_recent_with_enough_regions():
    dates = ["2024-01-01"] * 15 + ["2024-01-08"] * 15
    regions = [f"r{i}" for i in range(15)] * 2
    df = _make_region_df(dates, regions)
    result = estimate_cutoff_date(df, min_regions=10)
    assert result == pd.Timestamp("2024-01-08")


def test_estimate_cutoff_returns_earlier_date_when_latest_insufficient():
    dates = ["2024-01-01"] * 15 + ["2024-01-08"] * 3
    regions = [f"r{i}" for i in range(15)] + [f"r{i}" for i in range(3)]
    df = _make_region_df(dates, regions)
    result = estimate_cutoff_date(df, min_regions=10)
    assert result == pd.Timestamp("2024-01-01")


def test_estimate_cutoff_returns_none_when_never_enough():
    dates = ["2024-01-01"] * 3
    regions = ["r1", "r2", "r3"]
    df = _make_region_df(dates, regions)
    result = estimate_cutoff_date(df, min_regions=10)
    assert result is None


def test_estimate_cutoff_empty_dataframe():
    # An empty DataFrame has no dates to iterate → returns None.
    df = _make_region_df([], [])
    result = estimate_cutoff_date(df, min_regions=10)
    assert result is None


# ---------------------------------------------------------------------------
# compute_prediction_dates
# ---------------------------------------------------------------------------


def test_compute_prediction_dates_returns_up_to_4():
    cutoff = pd.Timestamp("2024-01-01")
    pred_upto = pd.Timestamp("2024-02-01")
    result = compute_prediction_dates(cutoff, pred_upto)
    assert len(result) == 4


def test_compute_prediction_dates_capped_at_4_when_range_is_wider():
    # 6 weeks between cutoff and pred_upto — must still return at most 4
    cutoff = pd.Timestamp("2024-01-01")
    pred_upto = pd.Timestamp("2024-02-12")  # 6 weeks later
    result = compute_prediction_dates(cutoff, pred_upto)
    assert len(result) == 4


def test_compute_prediction_dates_format():
    cutoff = pd.Timestamp("2024-01-01")
    pred_upto = pd.Timestamp("2024-01-29")
    result = compute_prediction_dates(cutoff, pred_upto)
    assert len(result) == 4
    for d in result:
        pd.Timestamp(d)  # should not raise
    # Dates must be weekly and strictly after cutoff
    ts_dates = [pd.Timestamp(d) for d in result]
    assert all(d > cutoff for d in ts_dates)
    assert all(d <= pred_upto for d in ts_dates)


def test_compute_prediction_dates_same_cutoff_and_pred():
    cutoff = pred_upto = pd.Timestamp("2024-01-01")
    result = compute_prediction_dates(cutoff, pred_upto)
    assert result == []


def test_compute_prediction_dates_one_week():
    cutoff = pd.Timestamp("2024-01-01")
    pred_upto = pd.Timestamp("2024-01-08")
    result = compute_prediction_dates(cutoff, pred_upto)
    assert len(result) == 1


# ---------------------------------------------------------------------------
# identify_cutoff_dates
# ---------------------------------------------------------------------------


def test_identify_cutoff_raises_when_none():
    with pytest.raises(ValueError, match="None"):
        identify_cutoff_dates(None, pd.Timestamp("2024-01-01"))

    with pytest.raises(ValueError, match="None"):
        identify_cutoff_dates(pd.Timestamp("2024-01-01"), None)


def test_identify_cutoff_case_is_binding():
    # case_cutoff < weather_cutoff + 28 days → cutoff = case_cutoff
    cutoff_case = pd.Timestamp("2024-01-10")
    cutoff_weather = pd.Timestamp("2024-01-01")
    cutoff, pred_upto, _ = identify_cutoff_dates(cutoff_case, cutoff_weather)
    assert cutoff == cutoff_case
    # pred_upto = weather_cutoff + 28, which is > cutoff_case
    from datetime import timedelta

    assert pred_upto == cutoff_weather + timedelta(28)
    assert pred_upto > cutoff_case


def test_identify_cutoff_weather_is_binding():
    # case_cutoff >= weather_cutoff + 28 days → cutoff = weather_cutoff + 28
    cutoff_case = pd.Timestamp("2024-03-01")
    cutoff_weather = pd.Timestamp("2024-01-01")
    cutoff, _, _ = identify_cutoff_dates(cutoff_case, cutoff_weather)
    from datetime import timedelta

    assert cutoff == cutoff_weather + timedelta(28)


def test_identify_cutoff_exact_28_day_boundary():
    # cutoff_case == cutoff_weather + 28 days exactly — condition is strict <,
    # so weather branch is binding at this boundary.
    from datetime import timedelta

    cutoff_weather = pd.Timestamp("2024-01-01")
    cutoff_case = cutoff_weather + timedelta(28)  # exactly 28 days
    cutoff, _, _ = identify_cutoff_dates(cutoff_case, cutoff_weather)
    assert cutoff == cutoff_weather + timedelta(28)


def test_identify_cutoff_returns_prediction_dates_list():
    cutoff_case = pd.Timestamp("2024-01-10")
    cutoff_weather = pd.Timestamp("2024-01-01")
    _, pred_upto, pred_dates = identify_cutoff_dates(cutoff_case, cutoff_weather)
    assert isinstance(pred_dates, list)
    # All prediction dates must be after the cutoff and at or before pred_upto
    for d in pred_dates:
        ts = pd.Timestamp(d)
        assert ts > cutoff_case
        assert ts <= pred_upto

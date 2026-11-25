from __future__ import annotations


import pandas as pd
import pytest

from pipelines.dengue_monitoring.metrics import baseline, compare, score


from .helpers import observations


def test_weekly_matches_daily_scoring_and_keeps_missing_weeks():
    forecasts = observations([0, 0, 0, 0, 0]).drop(
        columns=["signed_error", "actual_cases", "status"]
    )
    forecasts["target_start"] = forecasts.target_end - pd.Timedelta(days=6)
    forecasts["predicted_cases"] = 14
    weekly = forecasts[["region_id", "target_start", "target_end"]].copy()
    weekly["actual_cases"] = [7, 7, 0, 7, 7]
    daily = pd.DataFrame(
        {
            "region_id": "001",
            "date": pd.date_range("2025-01-01", "2025-02-04"),
            "case_count": 1,
        }
    )
    daily.loc[daily.date.between("2025-01-15", "2025-01-21"), "case_count"] = 0
    w = score(forecasts, weekly, "2025-02-01", frequency="weekly")
    d = score(forecasts, daily, "2025-02-01")
    pd.testing.assert_frame_equal(
        w[["status", "signed_error", "actual_cases"]],
        d[["status", "signed_error", "actual_cases"]],
    )
    missing = score(forecasts, weekly.drop(index=1), "2025-02-01", frequency="weekly")
    assert missing.status.tolist() == [
        "evaluated",
        "missing_actual",
        "zero_actual",
        "evaluated",
        "pending",
    ]
    flagged = compare(missing, baseline(observations([0])))
    assert not flagged.alert.any()


def test_weekly_rejects_shifted_windows_duplicates_and_invalid_lengths():
    forecasts = observations([0]).drop(
        columns=["signed_error", "actual_cases", "status"]
    )
    forecasts["target_start"] = forecasts.target_end - pd.Timedelta(days=6)
    forecasts["predicted_cases"] = 10
    weekly = forecasts[["region_id", "target_start", "target_end"]].assign(
        actual_cases=7
    )
    shifted = weekly.copy()
    shifted[["target_start", "target_end"]] += pd.Timedelta(days=2)
    with pytest.raises(ValueError, match="alignment mismatch"):
        score(forecasts, shifted, "2025-02-01", frequency="weekly")
    with pytest.raises(ValueError, match="Duplicate weekly"):
        score(forecasts, pd.concat([weekly, weekly]), "2025-02-01", frequency="weekly")
    with pytest.raises(ValueError, match="seven inclusive"):
        score(
            forecasts,
            weekly.assign(target_end=weekly.target_end + pd.Timedelta(days=1)),
            "2025-02-01",
            frequency="weekly",
        )


def test_exact_windows_sparse_zeros_unknown_region_and_pending():
    daily = pd.DataFrame(
        {
            "region_id": ["001", "001", "001"],
            "date": ["2024-12-28", "2025-01-01", "2025-01-14"],
            "case_count": [10, 2, 0],
        }
    )
    forecasts = pd.DataFrame(
        [
            {
                "region_id": region,
                "target_start": start,
                "target_end": end,
                "predicted_cases": 4,
            }
            for region, start, end in [
                ("001", "2024-12-29", "2025-01-04"),
                ("001", "2025-01-05", "2025-01-11"),
                ("unknown", "2025-01-05", "2025-01-11"),
                ("001", "2025-01-12", "2025-01-18"),
                ("001", "2024-12-20", "2024-12-26"),
            ]
        ]
    )
    out = score(forecasts, daily, "2025-01-15")
    assert out.status.tolist() == [
        "evaluated",
        "zero_actual",
        "missing_actual",
        "pending",
        "missing_actual",
    ]
    assert out.signed_error.iloc[0] == 1
    assert out.signed_error.iloc[1:].isna().all()
    with pytest.raises(ValueError, match="Duplicate"):
        score(forecasts, pd.concat([daily, daily]), "2025-01-15")


@pytest.mark.parametrize("frequency", ["daily", "weekly"])
@pytest.mark.parametrize(
    "prediction,expected", [(21, 0.5), (7, -0.5), (14, 0.0), (-7, -1.5)]
)
def test_signed_error_uses_actual_count(frequency, prediction, expected):
    forecasts = pd.DataFrame(
        {
            "region_id": ["001"],
            "target_start": [pd.Timestamp("2025-01-01")],
            "target_end": [pd.Timestamp("2025-01-07")],
            "predicted_cases": [prediction],
        }
    )
    actuals = (
        pd.DataFrame(
            {
                "region_id": ["001"] * 7,
                "date": pd.date_range("2025-01-01", periods=7),
                "case_count": [2] * 7,
            }
        )
        if frequency == "daily"
        else forecasts[["region_id", "target_start", "target_end"]].assign(
            actual_cases=14
        )
    )
    row = score(forecasts, actuals, "2025-01-07", frequency=frequency).iloc[0]
    assert row.actual_cases == 14
    assert row.signed_error == expected
    assert row.status == "evaluated"
    assert bool(row.negative_prediction) == (prediction < 0)


@pytest.mark.parametrize("frequency", ["daily", "weekly"])
def test_missing_prediction_is_unscored(frequency):
    forecasts = pd.DataFrame(
        {
            "region_id": ["001"],
            "target_start": [pd.Timestamp("2025-01-01")],
            "target_end": [pd.Timestamp("2025-01-07")],
            "predicted_cases": [float("nan")],
        }
    )
    actuals = (
        pd.DataFrame(
            {
                "region_id": ["001"] * 7,
                "date": pd.date_range("2025-01-01", periods=7),
                "case_count": [1] * 7,
            }
        )
        if frequency == "daily"
        else forecasts[["region_id", "target_start", "target_end"]].assign(
            actual_cases=7
        )
    )
    row = score(forecasts, actuals, "2025-01-07", frequency=frequency).iloc[0]
    assert row.status == "missing_prediction" and row.actual_cases == 7
    assert pd.isna(row.signed_error)


@pytest.mark.parametrize("frequency", ["daily", "weekly"])
@pytest.mark.parametrize("invalid", [-1, float("inf")])
def test_invalid_actual_counts_are_rejected(frequency, invalid):
    forecasts = pd.DataFrame(
        {
            "region_id": ["001"],
            "target_start": [pd.Timestamp("2025-01-01")],
            "target_end": [pd.Timestamp("2025-01-07")],
            "predicted_cases": [10],
        }
    )
    actuals = (
        pd.DataFrame(
            {
                "region_id": ["001"] * 7,
                "date": pd.date_range("2025-01-01", periods=7),
                "case_count": [invalid] * 7,
            }
        )
        if frequency == "daily"
        else forecasts[["region_id", "target_start", "target_end"]].assign(
            actual_cases=invalid
        )
    )
    with pytest.raises(ValueError, match="finite and non-negative"):
        score(forecasts, actuals, "2025-01-07", frequency=frequency)

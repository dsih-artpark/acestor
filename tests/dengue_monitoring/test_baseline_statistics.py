from __future__ import annotations


import pandas as pd
import pytest

from pipelines.dengue_monitoring.metrics import baseline


from .helpers import observations


def test_statistics_and_exclusions_against_hand_calculated_values():
    errors = observations([-1, 0, 1, 3, float("nan")])
    errors.loc[4, ["actual_cases", "status"]] = [0, "zero_actual"]
    row = baseline(errors).iloc[0]
    assert row.n == 4 and row.total == 5
    assert row.excluded == row.zero_actual == 1
    assert row["mean"] == 0.75
    assert row.sd == pytest.approx((35 / 12) ** 0.5)
    # Nearest rank gives p75=1; interpolation would give 1.5.
    assert [row[f"p{q:02}"] for q in (5, 10, 25, 50, 75, 90, 95)] == [
        -1,
        -1,
        -1,
        0,
        1,
        3,
        3,
    ]
    assert row.mean_minus_1sd == pytest.approx(0.75 - (35 / 12) ** 0.5)
    assert row.mean_plus_2sd == pytest.approx(0.75 + 2 * (35 / 12) ** 0.5)


def test_single_observation_has_percentiles_but_no_sample_sd():
    row = baseline(observations([0.25])).iloc[0]
    assert row.n == 1 and row["mean"] == row.p05 == row.p75 == row.p95 == 0.25
    assert pd.isna(row.sd) and pd.isna(row.mean_plus_1sd)


def test_no_valid_errors_produces_unavailable_reference():
    errors = observations([float("nan")] * 3)
    errors["status"] = ["zero_actual", "missing_actual", "missing_prediction"]
    errors["actual_cases"] = [0, float("nan"), 10]
    row = baseline(errors).iloc[0]
    assert row.n == 0 and row.total == row.excluded == 3 and row.zero_actual == 1
    assert pd.isna(row.p75) and pd.isna(row["mean"]) and pd.isna(row.sd)


def test_regions_and_years_pool_without_equal_year_weighting():
    older = observations([0, 1, 2], ["2024-01-07", "2024-01-14", "2024-01-21"])
    newer = observations([10], ["2025-01-07"]).assign(region_id="002")
    ref = baseline(pd.concat([older, newer], ignore_index=True))
    assert len(ref) == 1
    assert ref.iloc[0].n == 4 and ref.iloc[0].p75 == 2
    assert ref.iloc[0]["mean"] == 3.25


@pytest.mark.parametrize(
    "column,value",
    [
        ("scope", "another"),
        ("spatial_scale", "zone"),
        ("derivation", "rollup"),
        ("model", "xgb"),
        ("lead", 2),
    ],
)
def test_reference_statistics_do_not_mix_groups(column, value):
    first = observations([0, 1, 2, 3])
    second = observations([10, 11, 12, 13]).assign(**{column: value})
    ref = baseline(pd.concat([first, second], ignore_index=True))
    assert sorted(ref.p75.tolist()) == [2, 12]
    assert ref.n.tolist() == [4, 4]

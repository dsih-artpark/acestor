"""Tests for predictionMin / predictionMax on the ensemble output (issue #84).

Semantic being enforced: min and max are the extremes across per-model
predictions that voted into the ensemble. Not a confidence interval.
"""

from __future__ import annotations

import pandas as pd

from pipelines.dengue.steps.train_and_predict import _add_prediction_range


def _per_model_row(
    *, region: str, week: str, method: str, model: str, prediction: float
) -> dict:
    """A per-model row after zone/threshold merge — enough columns to group on."""
    return {
        "regionID": region,
        "startDatePredictedWeek": week,
        "thresholdMethod": method,
        "Mean": 1.0,
        "StdDev": 0.5,
        "prediction": prediction,
        "model": model,
    }


def test_three_models_min_max_across_members():
    per_model_dfs = [
        pd.DataFrame(
            [
                _per_model_row(
                    region="r1",
                    week="2026-07-06",
                    method="historical",
                    model="rf",
                    prediction=3.0,
                ),
                _per_model_row(
                    region="r2",
                    week="2026-07-06",
                    method="historical",
                    model="rf",
                    prediction=7.0,
                ),
            ]
        ),
        pd.DataFrame(
            [
                _per_model_row(
                    region="r1",
                    week="2026-07-06",
                    method="historical",
                    model="xgb",
                    prediction=5.0,
                ),
                _per_model_row(
                    region="r2",
                    week="2026-07-06",
                    method="historical",
                    model="xgb",
                    prediction=9.0,
                ),
            ]
        ),
        pd.DataFrame(
            [
                _per_model_row(
                    region="r1",
                    week="2026-07-06",
                    method="historical",
                    model="tse",
                    prediction=1.0,
                ),
                _per_model_row(
                    region="r2",
                    week="2026-07-06",
                    method="historical",
                    model="tse",
                    prediction=8.0,
                ),
            ]
        ),
    ]
    ensembled = pd.DataFrame(
        [
            _per_model_row(
                region="r1",
                week="2026-07-06",
                method="historical",
                model="ensembleModel",
                prediction=3.0,  # mean of 3,5,1
            ),
            _per_model_row(
                region="r2",
                week="2026-07-06",
                method="historical",
                model="ensembleModel",
                prediction=8.0,  # mean of 7,9,8
            ),
        ]
    )

    result = _add_prediction_range(ensembled, per_model_dfs)

    r1 = result[result["regionID"] == "r1"].iloc[0]
    r2 = result[result["regionID"] == "r2"].iloc[0]
    assert r1["predictionMin"] == 1.0
    assert r1["predictionMax"] == 5.0
    assert r2["predictionMin"] == 7.0
    assert r2["predictionMax"] == 9.0
    assert r1["prediction"] == 3.0
    assert r2["prediction"] == 8.0


def test_single_model_min_equals_max_equals_prediction():
    per_model_dfs = [
        pd.DataFrame(
            [
                _per_model_row(
                    region="r1",
                    week="2026-07-06",
                    method="historical",
                    model="rf",
                    prediction=2.5,
                ),
            ]
        ),
    ]
    ensembled = pd.DataFrame(
        [
            _per_model_row(
                region="r1",
                week="2026-07-06",
                method="historical",
                model="ensembleModel",
                prediction=2.5,
            ),
        ]
    )

    result = _add_prediction_range(ensembled, per_model_dfs)
    row = result.iloc[0]
    assert row["predictionMin"] == row["predictionMax"] == row["prediction"] == 2.5


def test_multiple_weeks_and_methods_each_grouped_independently():
    """Range is per (region × week × threshold_method) — verify grouping keys."""
    per_model_dfs = [
        pd.DataFrame(
            [
                _per_model_row(
                    region="r1",
                    week="2026-07-06",
                    method="historical",
                    model="rf",
                    prediction=1.0,
                ),
                _per_model_row(
                    region="r1",
                    week="2026-07-06",
                    method="previousNweeks",
                    model="rf",
                    prediction=10.0,
                ),
                _per_model_row(
                    region="r1",
                    week="2026-07-13",
                    method="historical",
                    model="rf",
                    prediction=2.0,
                ),
            ]
        ),
        pd.DataFrame(
            [
                _per_model_row(
                    region="r1",
                    week="2026-07-06",
                    method="historical",
                    model="xgb",
                    prediction=3.0,
                ),
                _per_model_row(
                    region="r1",
                    week="2026-07-06",
                    method="previousNweeks",
                    model="xgb",
                    prediction=20.0,
                ),
                _per_model_row(
                    region="r1",
                    week="2026-07-13",
                    method="historical",
                    model="xgb",
                    prediction=4.0,
                ),
            ]
        ),
    ]
    ensembled = pd.DataFrame(
        [
            _per_model_row(
                region="r1",
                week="2026-07-06",
                method="historical",
                model="ensembleModel",
                prediction=2.0,
            ),
            _per_model_row(
                region="r1",
                week="2026-07-06",
                method="previousNweeks",
                model="ensembleModel",
                prediction=15.0,
            ),
            _per_model_row(
                region="r1",
                week="2026-07-13",
                method="historical",
                model="ensembleModel",
                prediction=3.0,
            ),
        ]
    )

    result = (
        _add_prediction_range(ensembled, per_model_dfs)
        .sort_values(["startDatePredictedWeek", "thresholdMethod"])
        .reset_index(drop=True)
    )

    row = result.iloc[0]
    assert row["thresholdMethod"] == "historical"
    assert row["startDatePredictedWeek"] == "2026-07-06"
    assert row["predictionMin"] == 1.0
    assert row["predictionMax"] == 3.0

    row = result.iloc[1]
    assert row["thresholdMethod"] == "previousNweeks"
    assert row["predictionMin"] == 10.0
    assert row["predictionMax"] == 20.0

    row = result.iloc[2]
    assert row["startDatePredictedWeek"] == "2026-07-13"
    assert row["predictionMin"] == 2.0
    assert row["predictionMax"] == 4.0


def test_prediction_column_is_never_modified():
    """The existing prediction column (ensemble mean) must stay untouched."""
    per_model_dfs = [
        pd.DataFrame(
            [
                _per_model_row(
                    region="r1",
                    week="2026-07-06",
                    method="historical",
                    model="rf",
                    prediction=100.0,
                )
            ]
        ),
        pd.DataFrame(
            [
                _per_model_row(
                    region="r1",
                    week="2026-07-06",
                    method="historical",
                    model="xgb",
                    prediction=200.0,
                )
            ]
        ),
    ]
    ensembled = pd.DataFrame(
        [
            _per_model_row(
                region="r1",
                week="2026-07-06",
                method="historical",
                model="ensembleModel",
                prediction=42.42,
            )
        ]
    )
    result = _add_prediction_range(ensembled, per_model_dfs)
    assert result.iloc[0]["prediction"] == 42.42
    assert result.iloc[0]["predictionMin"] == 100.0
    assert result.iloc[0]["predictionMax"] == 200.0


def test_pre_existing_range_columns_are_ignored_in_grouping():
    """If a caller pre-populated predictionMin/max on per-model dfs, grouping
    must not include them — otherwise the group would fracture."""
    per_model_dfs = [
        pd.DataFrame(
            [
                {
                    **_per_model_row(
                        region="r1",
                        week="2026-07-06",
                        method="historical",
                        model="rf",
                        prediction=3.0,
                    ),
                    "predictionMin": 3.0,
                    "predictionMax": 3.0,
                }
            ]
        ),
        pd.DataFrame(
            [
                {
                    **_per_model_row(
                        region="r1",
                        week="2026-07-06",
                        method="historical",
                        model="xgb",
                        prediction=5.0,
                    ),
                    "predictionMin": 5.0,
                    "predictionMax": 5.0,
                }
            ]
        ),
    ]
    ensembled = pd.DataFrame(
        [
            _per_model_row(
                region="r1",
                week="2026-07-06",
                method="historical",
                model="ensembleModel",
                prediction=4.0,
            )
        ]
    )
    result = _add_prediction_range(ensembled, per_model_dfs)
    assert len(result) == 1
    row = result.iloc[0]
    assert row["predictionMin"] == 3.0
    assert row["predictionMax"] == 5.0

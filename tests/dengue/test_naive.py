"""Tests for the opt-in naive persistence forecasting model."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pandas as pd
import pytest

from pipelines.dengue.lib.maps import MODEL_FULL_NAME, MODEL_LABEL
from pipelines.dengue.lib.models import BaseModel, ModelContext, get_model
from pipelines.dengue.lib.models.naive import (
    MODEL_NAME,
    naive_persistence_forecast,
)


def _cases() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "district": ["a", "a", "a", "b", "b"],
            "recordDate": pd.to_datetime(
                [
                    "2026-06-26",
                    "2026-07-03",
                    "2026-07-17",  # after cutoff; must not leak
                    "2026-06-19",
                    "2026-06-26",  # stale relative to the shared cutoff
                ]
            ),
            "case": [3, 7, 99, 2, 4],
        }
    )


def test_forecast_repeats_each_regions_latest_value_for_every_target():
    out = naive_persistence_forecast(
        case_df=_cases(),
        spatial_col="district",
        cutoff_case=pd.Timestamp("2026-07-03"),
        prediction_dates=["2026-07-10", "2026-07-17", "2026-07-24"],
    )

    assert len(out) == 6
    assert out.groupby("district")["prediction"].unique().apply(list).to_dict() == {
        "a": [7.0],
        "b": [4.0],
    }
    assert set(out["recordDate"]) == set(
        pd.to_datetime(["2026-07-10", "2026-07-17", "2026-07-24"])
    )
    assert (out["model"] == MODEL_NAME).all()


def test_forecast_ignores_observations_after_cutoff():
    out = naive_persistence_forecast(
        case_df=_cases(),
        spatial_col="district",
        cutoff_case=pd.Timestamp("2026-07-03"),
        prediction_dates=["2026-07-10"],
    )

    a_prediction = out.loc[out["district"] == "a", "prediction"].iloc[0]
    assert a_prediction == 7.0
    assert a_prediction != 99.0


def test_forecast_warns_when_region_uses_older_observation(caplog):
    with caplog.at_level(logging.WARNING):
        naive_persistence_forecast(
            case_df=_cases(),
            spatial_col="district",
            cutoff_case=pd.Timestamp("2026-07-03"),
            prediction_dates=["2026-07-10"],
        )

    assert (
        "1 region(s) use their latest observation before cutoff_case=2026-07-03"
        in caplog.text
    )
    assert "maximum staleness=7 day(s)" in caplog.text
    assert "'b'" in caplog.text


def test_forecast_warns_and_skips_region_without_pre_cutoff_observation(caplog):
    cases = pd.concat(
        [
            _cases(),
            pd.DataFrame(
                {
                    "district": ["c"],
                    "recordDate": pd.to_datetime(["2026-07-10"]),
                    "case": [5],
                }
            ),
        ],
        ignore_index=True,
    )

    with caplog.at_level(logging.WARNING):
        out = naive_persistence_forecast(
            case_df=cases,
            spatial_col="district",
            cutoff_case=pd.Timestamp("2026-07-03"),
            prediction_dates=["2026-07-10"],
        )

    assert set(out["district"]) == {"a", "b"}
    assert "1 region(s) have no usable case observation" in caplog.text
    assert "'c'" in caplog.text


def test_empty_targets_returns_empty_contract():
    out = naive_persistence_forecast(
        case_df=pd.DataFrame(),
        spatial_col="district",
        cutoff_case=pd.Timestamp("2026-07-03"),
        prediction_dates=[],
    )

    assert out.empty
    assert out.columns.tolist() == ["district", "recordDate", "prediction", "model"]


def test_no_usable_history_returns_empty_contract():
    out = naive_persistence_forecast(
        case_df=pd.DataFrame(
            {
                "district": ["a"],
                "recordDate": pd.to_datetime(["2026-07-10"]),
                "case": [3],
            }
        ),
        spatial_col="district",
        cutoff_case=pd.Timestamp("2026-07-03"),
        prediction_dates=["2026-07-10"],
    )

    assert out.empty
    assert out.columns.tolist() == ["district", "recordDate", "prediction", "model"]


@pytest.mark.parametrize("prediction_date", ["2026-07-03", "2026-07-09"])
def test_rejects_non_future_or_off_grid_target(prediction_date: str):
    with pytest.raises(ValueError, match="not strictly after|off the weekly grid"):
        naive_persistence_forecast(
            case_df=_cases(),
            spatial_col="district",
            cutoff_case=pd.Timestamp("2026-07-03"),
            prediction_dates=[prediction_date],
        )


def test_model_is_registered_and_uses_pipeline_context():
    model = get_model("naive")
    assert isinstance(model, BaseModel)

    ctx = ModelContext(
        merged_df=pd.DataFrame(),
        case_df=_cases(),
        cfg=SimpleNamespace(spatial_res="district"),
        pred_upto=pd.Timestamp("2026-07-17"),
        cutoff_case=pd.Timestamp("2026-07-03"),
        prediction_dates=["2026-07-10", "2026-07-17"],
    )
    out = model.predict(ctx)

    assert len(out) == 4
    assert model.threshold_to_date(ctx) == pd.Timestamp("2026-07-03")


def test_report_and_map_model_names_are_registered():
    assert MODEL_FULL_NAME["naive"] == MODEL_NAME
    assert MODEL_LABEL[MODEL_NAME] == "Naive Persistence"

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from acestor import PipelineConfig, PipelineContext, PipelineRunner
from pipelines.dengue_monitoring import inputs
from pipelines.dengue_monitoring.metrics import score
from pipelines.dengue_monitoring.pipeline import build_pipeline
from pipelines.dengue_monitoring.actuals import load_actuals


from .helpers import archive, fixture_config


def test_consolidated_file_retains_boundary_leads_and_deduplicates(tmp_path):
    path = tmp_path / "predictions.csv"
    frame = pd.DataFrame(
        {
            "regionID": ["001", "001"],
            "model": ["randomForestRegression"] * 2,
            "startDatePredictedWeek": ["2026-01-04", "2026-01-11"],
            "issue_date": ["2025-12-15"] * 2,
            "lead": [3, 4],
            "predictionRaw": [10, 20],
        }
    )
    pd.concat([frame, frame]).to_csv(path, index=False)
    scope = {"name": "test", "spatial_scale": "zone", "models": ["rf"]}
    out = inputs.load_prediction_file(scope, path, ["001", "002"])
    assert out.lead.tolist() == [3, 3, 4, 4]
    assert out.predicted_cases.notna().sum() == 2
    assert out.issue_date.eq(pd.Timestamp("2025-12-15")).all()
    assert out.target_start.min() == pd.Timestamp("2025-12-29")
    frame.loc[1, "lead"] = 2
    frame.to_csv(path, index=False)
    with pytest.raises(ValueError, match="Inconsistent lead"):
        inputs.load_prediction_file(scope, path, ["001"])


def test_consolidated_file_rejects_conflicting_predictions(tmp_path):
    path = tmp_path / "predictions.csv"
    pd.DataFrame(
        {
            "regionID": ["001", "001"],
            "model": ["rf", "rf"],
            "startDatePredictedWeek": ["2026-01-04"] * 2,
            "issue_date": ["2025-12-29"] * 2,
            "lead": [1, 1],
            "predictionRaw": [1, 2],
        }
    ).to_csv(path, index=False)
    with pytest.raises(ValueError, match="Conflicting duplicate"):
        inputs.load_prediction_file(
            {"name": "test", "spatial_scale": "zone", "models": ["rf"]}, path, ["001"]
        )


def test_negative_raw_predictions_preserved_and_identified(tmp_path):
    path = tmp_path / "predictions.csv"
    pd.DataFrame(
        {
            "regionID": ["001"],
            "model": ["xgboostRegression"],
            "startDatePredictedWeek": ["2026-01-04"],
            "issue_date": ["2025-12-29"],
            "lead": [1],
            "predictionRaw": [-0.5],
        }
    ).to_csv(path, index=False)
    forecasts = inputs.load_prediction_file(
        {"name": "test", "spatial_scale": "zone", "models": ["xgb"]}, path, ["001"]
    )
    weekly = forecasts[["region_id", "target_start", "target_end"]].assign(
        actual_cases=2
    )
    result = score(forecasts, weekly, "2026-01-04", frequency="weekly")
    assert result.negative_prediction.iloc[0]
    assert result.signed_error.iloc[0] == -1.25


@pytest.mark.parametrize("label,day", [("start", "2025-01-01"), ("end", "2025-01-07")])
def test_sampled_actuals_column_mapping(tmp_path, label, day):
    path = tmp_path / "sampled.csv"
    pd.DataFrame(
        {"location.admin3.ID": ["001"], "metadata.primaryDate": [day], "case": [12]}
    ).to_csv(path, index=False)
    config = {
        "frequency": "weekly",
        "path": str(path),
        "region_column": "location.admin3.ID",
        "value_column": "case",
        "date_column": "metadata.primaryDate",
        "week_label": label,
    }
    data, _, frequency = load_actuals(config, Path("unused"))
    assert frequency == "weekly" and data.region_id.iloc[0] == "001"
    assert data.target_start.iloc[0] == pd.Timestamp("2025-01-01")
    assert data.target_end.iloc[0] == pd.Timestamp("2025-01-07")
    assert data.actual_cases.iloc[0] == 12
    del config["week_label"]
    with pytest.raises(ValueError, match="week_label"):
        load_actuals(config, Path("unused"))


def test_archive_dedup_dates_region_ids_and_missing_prediction(tmp_path):
    archive(tmp_path / "run", "2025-01-01", ["2025-01-08", "2025-01-15"])
    scope = {"name": "test", "spatial_scale": "ward", "models": ["rf"]}
    frame = inputs.load_forecasts(
        scope, {"issue_date": "2025-01-01"}, tmp_path / "run", ["001", "002"]
    )
    assert len(frame) == 4
    assert frame.model.unique().tolist() == ["rf"]
    assert frame.lead.tolist() == [1, 2, 1, 2]
    assert frame.target_start.iloc[0] == pd.Timestamp("2025-01-02")
    assert frame[frame.region_id == "002"].predicted_cases.isna().all()
    with pytest.raises(ValueError, match="issue_date"):
        inputs.load_forecasts(
            scope, {"issue_date": "2025-01-02"}, tmp_path / "run", ["001"]
        )
    path = tmp_path / "run/outputs/predictions.csv"
    data = pd.read_csv(path, dtype={"regionID": str})
    data.loc[0, "predictionRaw"] = 99
    data.to_csv(path, index=False)
    with pytest.raises(ValueError, match="Conflicting"):
        inputs.load_forecasts(
            scope, {"issue_date": "2025-01-01"}, tmp_path / "run", ["001"]
        )


def test_existing_backtests_reused_and_regeneration_isolated(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(inputs, "invoke", lambda *args: calls.append(args))
    scope = {"spatial_scale": "ward"}
    spec = {"issue_date": "2024-01-01", "run_dir": str(tmp_path / "old")}
    Path(spec["run_dir"]).mkdir()
    raw = {"model": {"models": ["rf"]}, "email": {"enabled": True}}
    assert inputs.resolve_historical_run(
        scope, spec, raw, tmp_path / "cache", True
    ) == Path(spec["run_dir"])
    assert not calls
    generated = inputs.resolve_historical_run(
        scope, spec, raw, tmp_path / "cache", False
    )
    assert generated != Path(spec["run_dir"])
    assert calls[0][1]["run"]["run_date"] == "2024-01-01"
    assert calls[0][1]["email"]["enabled"] is False
    assert raw["email"]["enabled"] is True
    assert (generated / "monitoring_success.json").exists()


def test_failed_backtest_not_cached_and_derived_latest_rejected(tmp_path, monkeypatch):
    def fail(*args):
        raise RuntimeError("failed")

    monkeypatch.setattr(inputs, "invoke", fail)
    with pytest.raises(RuntimeError):
        inputs.resolve_historical_run(
            {"spatial_scale": "zone"}, {"issue_date": "2024-01-01"}, {}, tmp_path, True
        )
    assert not list(tmp_path.rglob("monitoring_success.json"))
    with pytest.raises(ValueError, match="source_run_id"):
        inputs.resolve_historical_run(
            {"spatial_scale": "ward", "forecast_builder": "derived:build"},
            {"issue_date": "2024-01-01"},
            {"run": {"source_run_id": "latest"}},
            tmp_path,
            True,
        )


def test_generated_history_runs_once_then_reuses_cache(tmp_path, monkeypatch):
    raw = fixture_config(tmp_path)
    scope = raw["monitoring"]["scopes"][0]
    scope["historical"]["runs"] = [{"issue_date": "2024-01-01"}]
    raw["monitoring"]["backtest_dir"] = str(tmp_path / "backtests")
    calls = []

    def generate(builder, config, run_id):
        calls.append(config)
        path = Path(config["storages"]["artifacts"]["filesystem"]["base_path"]) / run_id
        archive(path, "2024-01-01", ["2024-01-08"], predicted=7)

    monkeypatch.setattr(inputs, "invoke", generate)
    raw["monitoring"]["mode"] = "build_baseline"
    for run in ("first", "second"):
        cfg = PipelineConfig(tmp_path / "monitor.yaml", raw)
        result = PipelineRunner(
            dag=build_pipeline(cfg),
            context=PipelineContext.from_config(cfg, run_id=run),
        ).run()
        assert result.status == "success", result.failure_detail
    assert len(calls) == 1
    assert calls[0]["run"]["run_date"] == "2024-01-01"


def test_derived_backtest_preserves_parent_and_clamps_case_history(
    tmp_path, monkeypatch
):
    archive(tmp_path / "source/parent", "2024-01-01", ["2024-01-08"])
    case_path = tmp_path / "cases.csv"
    pd.DataFrame(
        {
            "region_id": ["001", "001"],
            "date": ["2023-12-31", "2024-02-01"],
            "case_count": [1, 99],
        }
    ).to_csv(case_path, index=False)
    raw = {
        "run": {"source_run_id": "parent"},
        "storages": {
            "artifacts": {
                "kind": "filesystem",
                "filesystem": {"base_path": str(tmp_path / "source")},
            }
        },
        "downscale": {"cases_csv": str(case_path)},
    }
    calls = []
    monkeypatch.setattr(inputs, "invoke", lambda *args: calls.append(args))
    directory = inputs.resolve_historical_run(
        {
            "spatial_scale": "ward",
            "forecast_builder": "pipelines.dengue_downscale.pipeline:build_pipeline",
        },
        {"issue_date": "2024-01-01"},
        raw,
        tmp_path / "cache",
        True,
    )
    generated = calls[0][1]
    assert generated["run"]["source_run_id"] == str(tmp_path / "source/parent")
    assert len(pd.read_csv(generated["downscale"]["cases_csv"])) == 1
    assert len(pd.read_csv(case_path)) == 2
    assert (
        json.loads((directory / "sampling_day.json").read_text())["run_date"]
        == "2024-01-01"
    )

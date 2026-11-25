from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
import yaml

from acestor import PipelineConfig, PipelineContext, PipelineRunner
from pipelines.dengue_monitoring import inputs
from pipelines.dengue_monitoring.pipeline import build_pipeline, validate


from .helpers import fixture_config, execute_test, build_test_baseline


def test_consolidated_periods_full_pipeline_shared_file(tmp_path):
    raw = fixture_config(tmp_path)
    scope = raw["monitoring"]["scopes"][0]
    frames = []
    for spec in scope["historical"]["runs"] + scope["current_runs"]:
        frame = pd.read_csv(
            Path(spec["run_dir"]) / "outputs/predictions.csv", dtype={"regionID": str}
        )
        frame["issue_date"] = spec["issue_date"]
        frame["lead"] = 1
        frames.append(frame)
    path = tmp_path / "all_predictions.csv"
    pd.concat(frames).to_csv(path, index=False)
    scope["historical"] = {
        "start": "2024-01-01",
        "end": "2024-12-31",
        "predictions_file": str(path),
    }
    scope["current"] = {
        "start": "2025-01-01",
        "end": "2025-01-08",
        "predictions_file": str(path),
    }
    del scope["current_runs"]
    del scope["forecast_config"]
    scope["actuals"] = {
        "frequency": "daily",
        "path": str(tmp_path / "prepared/ward/cases_daily.csv"),
    }
    build_test_baseline(raw, tmp_path)
    config = PipelineConfig(tmp_path / "monitoring.yaml", raw)
    result = PipelineRunner(
        dag=build_pipeline(config),
        context=PipelineContext.from_config(config, run_id="combined"),
    ).run()
    assert result.status == "success", result.failure_detail
    output = tmp_path / "results/combined/outputs"
    assert not (output / "historical_errors.csv").exists()
    manifest = json.loads((output / "manifest.json").read_text())
    history = Path(manifest["baseline_directory"]) / "historical_errors.csv"
    assert len(pd.read_csv(history)) == 1
    assert all(
        "historical" not in entry and entry["role"] == "current"
        for entry in manifest["inputs"]
    )
    current = pd.read_csv(output / "current_errors.csv")
    assert len(current) == 1 and current.target_end.iloc[0] == "2025-01-08"
    scope["current"]["start"] = "2024-12-31"
    with pytest.raises(ValueError, match="Current period"):
        validate(raw)


def test_weekly_full_pipeline_without_daily_actuals(tmp_path):
    raw = fixture_config(tmp_path)
    actuals = tmp_path / "weekly.csv"
    pd.DataFrame(
        {
            "region_id": ["001"] * 3,
            "target_start": ["2024-01-02", "2025-01-02", "2025-01-09"],
            "target_end": ["2024-01-08", "2025-01-08", "2025-01-15"],
            "actual_cases": [7, 7, 7],
        }
    ).to_csv(actuals, index=False)
    raw["monitoring"]["scopes"][0]["actuals"] = {
        "frequency": "weekly",
        "path": str(actuals),
    }
    (tmp_path / "prepared/ward/cases_daily.csv").unlink()
    build_test_baseline(raw, tmp_path)
    cfg = PipelineConfig(tmp_path / "monitor.yaml", raw)
    result = PipelineRunner(
        dag=build_pipeline(cfg),
        context=PipelineContext.from_config(cfg, run_id="weekly"),
    ).run()
    assert result.status == "success", result.failure_detail
    output = tmp_path / "results/weekly"
    scored = pd.read_csv(output / "outputs/current_errors.csv")
    assert scored.alert.tolist() == [False, True]
    assert (output / "inputs/test/current/cases_weekly.csv").exists()
    manifest = json.loads((output / "outputs/manifest.json").read_text())
    assert all(i["actuals_frequency"] == "weekly" for i in manifest["inputs"])


def test_full_dag_outputs(tmp_path):
    raw = fixture_config(tmp_path)
    build_test_baseline(raw, tmp_path)
    config = PipelineConfig(tmp_path / "monitoring.yaml", raw)
    context = PipelineContext.from_config(config, run_id="monitor")
    result = PipelineRunner(dag=build_pipeline(config), context=context).run()
    assert result.status == "success", result.failure_detail
    output = tmp_path / "results/monitor/outputs"
    current = pd.read_csv(output / "current_errors.csv", dtype={"region_id": str})
    assert len(current) == 2 and current.alert.tolist() == [False, True]
    assert current.region_id.tolist() == ["001", "001"]
    assert len(pd.read_csv(output / "alerts.csv")) == 1
    assert "<svg" in (output / "report.html").read_text()
    assert "historical p75" in (output / "report.html").read_text()
    assert (tmp_path / "results/monitor/inputs/test/current/cases_daily.csv").exists()
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["baseline_id"] == current.baseline_id.iloc[0]


def test_case_refresh_uses_existing_components_without_weather():
    from pipelines.dengue_monitoring.pipeline import build_case_refresh_pipeline

    dag = build_case_refresh_pipeline(PipelineConfig(Path("unused"), {}))
    assert set(dag.topological_order()) == {
        "download_geojsons",
        "download_case_data",
        "parse_case_data",
    }
    assert dag.parents("parse_case_data") == {"download_geojsons", "download_case_data"}


def test_cli_entrypoint_with_synthetic_archives(tmp_path):
    raw = fixture_config(tmp_path)
    build_test_baseline(raw, tmp_path)
    inputs.invoke("pipelines.dengue_monitoring.pipeline:build_pipeline", raw, "cli")
    assert (tmp_path / "results/cli/outputs/report.html").exists()


def test_invalid_config_rejected_before_work(tmp_path):
    raw = fixture_config(tmp_path)
    raw["monitoring"]["reuse_existing_runs"] = "false"
    with pytest.raises(ValueError, match="boolean"):
        validate(raw)


def test_cli_clean_cannot_remove_existing_evaluation(tmp_path):
    import subprocess
    import sys

    raw = fixture_config(tmp_path)
    build_test_baseline(raw, tmp_path)
    result = execute_test(raw, tmp_path, "protected")
    assert result.status == "success", result.failure_detail
    run_dir = tmp_path / "results/protected"
    before = {
        path.relative_to(run_dir): path.read_bytes()
        for path in run_dir.rglob("*")
        if path.is_file()
    }
    config = tmp_path / "monitor.yaml"
    config.write_text(yaml.safe_dump(raw))
    command = [
        sys.executable,
        "-m",
        "acestor.run",
        "--pipeline",
        "pipelines.dengue_monitoring.pipeline:build_pipeline",
        "--config",
        str(config),
        "--run-id",
        "protected",
    ]
    for extra in ([], ["--clean"]):
        proc = subprocess.run(command + extra, capture_output=True, text=True)
        assert proc.returncode != 0
        expected = "--clean is not supported" if extra else "already been used"
        assert expected in proc.stdout + proc.stderr
        after = {
            path.relative_to(run_dir): path.read_bytes()
            for path in run_dir.rglob("*")
            if path.is_file()
        }
        assert after == before


def test_refresh_override_preserves_snapshot_and_evaluation_identity(
    tmp_path, monkeypatch
):
    from pipelines.dengue_monitoring import pipeline

    raw = fixture_config(tmp_path)
    build_test_baseline(raw, tmp_path)
    scope = raw["monitoring"]["scopes"][0]
    scope["current"] = {
        "start": "2025-01-01",
        "end": "2025-02-01",
        "refresh_actuals": False,
    }
    del raw["monitoring"]["refresh_actuals"]

    def unexpected(*args):
        pytest.fail("Explicit refresh_actuals=false must skip fetching")

    monkeypatch.setattr(pipeline, "refresh_cases", unexpected)
    manifests = []
    for run in ("one", "two"):
        result = execute_test(raw, tmp_path, run)
        assert result.status == "success", result.failure_detail
        manifests.append(
            json.loads((tmp_path / f"results/{run}/outputs/manifest.json").read_text())
        )
    assert manifests[0]["evaluation_sha256"] == manifests[1]["evaluation_sha256"]
    assert manifests[0]["run_sha256"] != manifests[1]["run_sha256"]


@pytest.mark.parametrize("consolidated", [False, True])
def test_current_unknown_regions_fail_unless_cohort_explicit(tmp_path, consolidated):
    raw = fixture_config(tmp_path)
    build_test_baseline(raw, tmp_path)
    scope = raw["monitoring"]["scopes"][0]
    path = tmp_path / "now1/outputs/predictions.csv"
    frame = pd.read_csv(path, dtype={"regionID": str})
    frame = pd.concat([frame, frame.assign(regionID="NEW")], ignore_index=True)
    if consolidated:
        frame["issue_date"] = "2025-01-01"
        frame["lead"] = 1
        scope.pop("current_runs")
        scope["current"] = {
            "start": "2025-01-01",
            "end": "2025-02-01",
            "predictions_file": str(path),
        }
    frame.to_csv(path, index=False)
    result = execute_test(raw, tmp_path, "unexpected")
    assert (
        result.status == "failed"
        and "outside evaluation cohort" in result.failure_detail
    )
    assert "NEW" in result.failure_detail
    scope["region_ids"] = ["001"]
    result = execute_test(raw, tmp_path, "selected")
    assert result.status == "success", result.failure_detail


def test_refresh_requires_enabled_download_and_rewritten_output(tmp_path, monkeypatch):
    prep = tmp_path / "prep.yaml"
    raw = {
        "data": {
            "region_type": "ward",
            "prepared_data": {"base_dir": str(tmp_path / "prepared")},
            "case_download": {"enabled": False},
        }
    }
    prep.write_text(yaml.safe_dump(raw))
    calls = []
    monkeypatch.setattr(inputs, "invoke", lambda *args: calls.append(args))
    with pytest.raises(ValueError, match="enabled is false"):
        inputs.refresh_cases(str(prep), tmp_path / "refresh")
    assert not calls
    raw["data"]["case_download"]["enabled"] = True
    prep.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="did not rewrite"):
        inputs.refresh_cases(str(prep), tmp_path / "refresh")
    daily = tmp_path / "prepared/ward/cases_daily.csv"
    daily.parent.mkdir(parents=True)
    daily.write_text("region_id,date,case_count\n001,2025-01-01,1\n")
    with pytest.raises(ValueError, match="did not rewrite"):
        inputs.refresh_cases(str(prep), tmp_path / "refresh")
    import os

    def access_only(*args):
        stat = daily.stat()
        os.utime(daily, ns=(stat.st_atime_ns + 1000000000, stat.st_mtime_ns))

    monkeypatch.setattr(inputs, "invoke", access_only)
    with pytest.raises(ValueError, match="did not rewrite"):
        inputs.refresh_cases(str(prep), tmp_path / "refresh")
    monkeypatch.setattr(
        inputs,
        "invoke",
        lambda *args: daily.write_text(
            "region_id,date,case_count\n001,2025-01-01,20\n"
        ),
    )
    assert inputs.refresh_cases(str(prep), tmp_path / "refresh") == {"ward"}


def test_refresh_wrong_scale_rejected(tmp_path, monkeypatch):
    from pipelines.dengue_monitoring import pipeline

    raw = fixture_config(tmp_path)
    build_test_baseline(raw, tmp_path)
    raw["monitoring"]["refresh_actuals"] = True
    raw["monitoring"]["scopes"][0]["prep_config"] = "unused.yaml"
    monkeypatch.setattr(pipeline, "refresh_cases", lambda *args: {"zone"})
    result = execute_test(raw, tmp_path, "wrong-scale")
    assert (
        result.status == "failed" and "requested scale: ward" in result.failure_detail
    )


def test_current_prep_does_not_require_old_forecast_config(tmp_path):
    raw = fixture_config(tmp_path)
    build_test_baseline(raw, tmp_path)
    scope = raw["monitoring"]["scopes"][0]
    prep = tmp_path / "prep.yaml"
    prep.write_text(
        yaml.safe_dump(
            {"data": {"prepared_data": {"base_dir": str(tmp_path / "prepared")}}}
        )
    )
    scope["prep_config"] = str(prep)
    Path(scope["forecast_config"]).unlink()
    result = execute_test(raw, tmp_path, "independent")
    assert result.status == "success", result.failure_detail

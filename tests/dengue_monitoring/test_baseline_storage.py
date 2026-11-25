from __future__ import annotations

import json

import pandas as pd
import pytest
import yaml


from .helpers import fixture_config, execute_test, build_test_baseline


def test_frozen_history_refresh_and_explicit_rebuild(tmp_path, monkeypatch):
    from pipelines.dengue_monitoring import pipeline, baselines

    raw = fixture_config(tmp_path)
    scope = raw["monitoring"]["scopes"][0]
    daily = tmp_path / "prepared/ward/cases_daily.csv"
    historical = tmp_path / "historical.csv"
    historical.write_bytes(daily.read_bytes())
    scope["historical"]["actuals"] = {"path": str(historical)}
    first = build_test_baseline(raw, tmp_path)
    frozen = baselines.load(raw["monitoring"])[2]["baseline.csv"]
    prep = tmp_path / "prep.yaml"
    prep.write_text(
        yaml.safe_dump(
            {"data": {"prepared_data": {"base_dir": str(daily.parent.parent)}}}
        )
    )
    scope["prep_config"] = str(prep)
    del raw["monitoring"]["refresh_actuals"]
    calls = []

    def refresh(*args):
        calls.append(args)
        frame = pd.read_csv(daily, dtype={"region_id": str})
        frame["case_count"] = len(calls) + 1
        frame.to_csv(daily, index=False)
        return {"ward"}

    monkeypatch.setattr(pipeline, "refresh_cases", refresh)
    historical.unlink()
    for run_id in ("current1", "current2"):
        result = execute_test(raw, tmp_path, run_id)
        assert result.status == "success", result.failure_detail
    assert len(calls) == 2
    assert baselines.load(raw["monitoring"])[2]["baseline.csv"] == frozen
    manifests = [
        json.loads((tmp_path / f"results/{run}/outputs/manifest.json").read_text())
        for run in ("current1", "current2")
    ]
    assert {m["baseline_id"] for m in manifests} == {first}
    assert manifests[0]["run_sha256"] != manifests[1]["run_sha256"]
    errors = [
        pd.read_csv(tmp_path / f"results/{run}/outputs/current_errors.csv")
        for run in ("current1", "current2")
    ]
    assert errors[0].signed_error.tolist() != errors[1].signed_error.tolist()
    historical.write_bytes(daily.read_bytes())
    second = build_test_baseline(raw, tmp_path, "recalibration")
    assert second != first
    raw["monitoring"]["baseline"]["id"] = first
    assert baselines.load(raw["monitoring"])[2]["baseline.csv"] == frozen


def test_missing_tampered_baseline_and_duplicate_run_are_rejected(tmp_path):
    raw = fixture_config(tmp_path)
    missing = execute_test(raw, tmp_path, "missing")
    assert missing.status == "failed" and "No frozen baseline" in missing.failure_detail
    identity = build_test_baseline(raw, tmp_path)
    result = execute_test(raw, tmp_path, "once")
    assert result.status == "success", result.failure_detail
    report = tmp_path / "results/once/outputs/report.html"
    before = report.read_bytes()
    result = execute_test(raw, tmp_path, "once")
    assert result.status == "failed" and "already been used" in result.failure_detail
    assert report.read_bytes() == before
    (tmp_path / "baselines" / identity / "baseline.csv").write_text("corrupted")
    result = execute_test(raw, tmp_path, "tampered")
    assert (
        result.status == "failed" and "integrity check failed" in result.failure_detail
    )


def test_baseline_contract_changes_require_explicit_rebuild(tmp_path):
    raw = fixture_config(tmp_path)
    build_test_baseline(raw, tmp_path)
    raw["monitoring"]["scopes"][0]["leads"] = [1]
    result = execute_test(raw, tmp_path, "changed")
    assert (
        result.status == "failed"
        and "differ from frozen baseline" in result.failure_detail
    )


def test_failed_recalibration_does_not_activate_new_baseline(tmp_path, monkeypatch):
    from acestor.io.storage import FileStorage
    from pipelines.dengue_monitoring import baselines

    raw = fixture_config(tmp_path)
    first = build_test_baseline(raw, tmp_path)
    daily = tmp_path / "prepared/ward/cases_daily.csv"
    frame = pd.read_csv(daily, dtype={"region_id": str})
    frame["case_count"] = 2
    frame.to_csv(daily, index=False)
    original = FileStorage.write

    def fail_output(self, content, path):
        if str(path).endswith("outputs/manifest.json"):
            raise OSError("simulated output failure")
        return original(self, content, path)

    # Fail the last required artifact before activation.
    monkeypatch.setattr(FileStorage, "write", fail_output)
    raw["monitoring"]["mode"] = "build_baseline"
    result = execute_test(raw, tmp_path, "failed-rebuild")
    assert (
        result.status == "failed"
        and "simulated output failure" in result.failure_detail
    )
    assert baselines.load(raw["monitoring"])[0] == first


@pytest.mark.parametrize("field", ["schema_version", "method_version"])
def test_unsupported_baseline_versions_rejected_with_valid_hash(tmp_path, field):
    from pipelines.dengue_monitoring import baselines

    raw = fixture_config(tmp_path)
    identity = build_test_baseline(raw, tmp_path)
    root = tmp_path / "baselines"
    manifest = json.loads((root / identity / "manifest.json").read_text())
    payload = manifest["identity"]
    target = payload if field == "schema_version" else payload["contract"]
    target[field] = 999
    changed = baselines.sha(baselines.canonical(payload))
    directory = root / changed
    directory.mkdir()
    manifest["baseline_id"] = changed
    (directory / "manifest.json").write_text(baselines.canonical(manifest))
    raw["monitoring"]["baseline"]["id"] = changed
    with pytest.raises(ValueError, match=f"Unsupported frozen baseline {field}"):
        baselines.load(raw["monitoring"])


def test_saved_baseline_round_trip_and_identical_rebuild(tmp_path):
    from pipelines.dengue_monitoring import baselines
    from pipelines.dengue_monitoring.pipeline import read_table

    raw = fixture_config(tmp_path)
    first = build_test_baseline(raw, tmp_path)
    identity, payload, files = baselines.load(raw["monitoring"])
    reference = read_table(files["baseline.csv"])
    assert identity == first and reference.iloc[0].p75 == 0
    assert reference.iloc[0].n == 1
    assert set(files) == {
        "baseline.csv",
        "historical_errors.csv",
        "inputs/test/historical/cases_daily.csv",
    }
    original_files = dict(files)
    second = build_test_baseline(raw, tmp_path, "same-inputs")
    assert second == first
    assert baselines.load(raw["monitoring"])[2] == original_files
    assert payload["contract"]["scopes"]["test"]["region_ids"] == ["001"]

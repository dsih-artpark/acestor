"""Slice 4 runner: input dataset sync + env forwarding wired in."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from acestor.remote.providers.base import CloudProvider, Lifecycle, RemoteHost
from acestor.remote.runner import RemoteRunOptions, run_remote


class FakeProvider(CloudProvider):
    name = "fake"

    def __init__(self, host: RemoteHost) -> None:
        self.host = host
        self.terminated = 0

    def provision(
        self, instance_type: str, lifecycle: Lifecycle, region: str, run_id: str = ""
    ) -> RemoteHost:
        return self.host

    def terminate(self, host: RemoteHost) -> None:
        self.terminated += 1


@pytest.fixture
def host() -> RemoteHost:
    return RemoteHost(
        id="i-fake",
        ip="10.0.0.5",
        ssh_user="ubuntu",
        ssh_key_path="/tmp/fake.pem",
        instance_type="t3.medium",
        lifecycle="on-demand",
        region="ap-south-1",
    )


def _write_config(repo: Path, name: str, body: dict) -> str:
    import yaml

    (repo / "configs").mkdir(parents=True, exist_ok=True)
    p = repo / "configs" / name
    p.write_text(yaml.safe_dump(body))
    return f"configs/{name}"


def _opts(repo: Path, host_provider, config_rel: str, **overrides) -> RemoteRunOptions:
    base = dict(
        pipeline="pipelines.dengue_prep.pipeline:build_pipeline",
        config=config_rel,
        run_id="slice4-r1",
        provider=host_provider,
        instance_type="t3.medium",
        lifecycle="on-demand",
        region="ap-south-1",
        ledger_path=repo / "runs.jsonl",
        repo_root=repo,
        artifacts_local_root=repo / "artifacts",
    )
    base.update(overrides)
    return RemoteRunOptions(**base)


def test_input_sync_called_when_local_dirs_exist(tmp_path, host):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\n")
    (repo / "ka_datasets" / "geojsons").mkdir(parents=True)
    cfg = _write_config(
        repo,
        "ka_district_prep.yaml",
        {"data": {"geojson": {"base_path": "ka_datasets/geojsons"}}},
    )
    provider = FakeProvider(host)
    opts = _opts(repo, provider, cfg)

    with (
        patch("acestor.remote.runner.sync_repo") as sync_repo,
        patch("acestor.remote.runner.sync_input_datasets") as sid,
        patch("acestor.remote.runner.ssh_exec", return_value=0),
        patch("acestor.remote.runner.sync_artifacts_back", return_value=0),
    ):
        run_remote(opts)

    sync_repo.assert_called_once()
    sid.assert_called_once()
    inputs = sid.call_args.args[1]
    assert len(inputs) == 1
    assert inputs[0].key_path == "data.geojson.base_path"


def test_input_sync_skipped_when_config_uses_dashboard_sources(tmp_path, host):
    """No local dirs exist → walker returns empty → no rsync call."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\n")
    cfg = _write_config(
        repo,
        "gba_ward_prep.yaml",
        {
            "data": {
                "geojson": {"base_path": "gba_datasets/geojsons"},  # doesn't exist
                "case_download": {"source_path": "gba_datasets/raw_case"},
            }
        },
    )
    provider = FakeProvider(host)
    opts = _opts(repo, provider, cfg)

    with (
        patch("acestor.remote.runner.sync_repo"),
        patch("acestor.remote.runner.sync_input_datasets") as sid,
        patch("acestor.remote.runner.ssh_exec", return_value=0),
        patch("acestor.remote.runner.sync_artifacts_back", return_value=0),
    ):
        run_remote(opts)

    sid.assert_not_called()  # walker returned []


def test_skip_input_sync_flag_bypasses_config_walk(tmp_path, host):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\n")
    (repo / "ka_datasets").mkdir()  # would normally be picked up
    cfg = _write_config(
        repo,
        "cfg.yaml",
        {"data": {"geojson": {"base_path": "ka_datasets"}}},
    )
    provider = FakeProvider(host)
    opts = _opts(repo, provider, cfg, skip_input_sync=True)

    with (
        patch("acestor.remote.runner.sync_repo"),
        patch("acestor.remote.runner.sync_input_datasets") as sid,
        patch("acestor.remote.runner.ssh_exec", return_value=0),
        patch("acestor.remote.runner.sync_artifacts_back", return_value=0),
    ):
        run_remote(opts)

    sid.assert_not_called()


def test_env_forwarding_prefixes_pipeline_command(tmp_path, host, monkeypatch):
    monkeypatch.setenv("DASHBOARD_URL", "https://dashboard.example")
    monkeypatch.setenv("DASHBOARD_CLIENT_ID", "cid")
    monkeypatch.delenv("DASHBOARD_CLIENT_SECRET", raising=False)

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\n")
    cfg = _write_config(repo, "cfg.yaml", {"data": {}})
    provider = FakeProvider(host)
    opts = _opts(
        repo,
        provider,
        cfg,
        forward_env=("DASHBOARD_URL", "DASHBOARD_CLIENT_ID", "DASHBOARD_CLIENT_SECRET"),
    )

    captured: list[str] = []

    def fake_exec(_h, cmd, stream=True):
        captured.append(cmd)
        return 0

    with (
        patch("acestor.remote.runner.sync_repo"),
        patch("acestor.remote.runner.ssh_exec", side_effect=fake_exec),
        patch("acestor.remote.runner.sync_artifacts_back", return_value=0),
    ):
        run_remote(opts)

    pipeline_cmd = next(c for c in captured if "acestor.run" in c)
    assert pipeline_cmd.startswith("export ")
    assert "export DASHBOARD_URL=https://dashboard.example;" in pipeline_cmd
    assert "export DASHBOARD_CLIENT_ID=cid;" in pipeline_cmd
    # unset var must NOT appear
    assert "DASHBOARD_CLIENT_SECRET" not in pipeline_cmd

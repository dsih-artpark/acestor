"""Slice 3 runner integration — sync + bootstrap + exec + artifact download,
with all subprocess calls mocked. Verifies the 6-step lifecycle sequence
and failure classification."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from acestor.remote.providers.base import CloudProvider, Lifecycle, RemoteHost
from acestor.remote.runner import RemoteRunOptions, run_remote


class FakeProvider(CloudProvider):
    """Real host (not the localhost mock path) so bootstrap/sync/exec run."""

    name = "fake"

    def __init__(self, host: RemoteHost) -> None:
        self.host = host
        self.provisioned = 0
        self.terminated = 0

    def provision(
        self, instance_type: str, lifecycle: Lifecycle, region: str
    ) -> RemoteHost:
        self.provisioned += 1
        return self.host

    def terminate(self, host: RemoteHost) -> None:
        self.terminated += 1


@pytest.fixture
def host() -> RemoteHost:
    # Non-loopback IP + a key path → runner takes the full slice-3 path
    return RemoteHost(
        id="i-fake",
        ip="10.0.0.5",
        ssh_user="ubuntu",
        ssh_key_path="/tmp/fake.pem",
        instance_type="t3.nano",
        lifecycle="on-demand",
        region="ap-south-1",
    )


def _opts(
    tmp_path: Path, host_provider: CloudProvider, **overrides
) -> RemoteRunOptions:
    base = dict(
        pipeline="pipelines.dengue.pipeline:build_pipeline",
        config="configs/ka_district.yaml",
        run_id="test-r1",
        provider=host_provider,
        instance_type="t3.nano",
        lifecycle="on-demand",
        region="ap-south-1",
        ledger_path=tmp_path / "runs.jsonl",
        repo_root=tmp_path / "repo",
        artifacts_local_root=tmp_path / "artifacts",
    )
    base.update(overrides)
    # Give the runner a repo dir + a stub config file to sync from
    (base["repo_root"]).mkdir(parents=True, exist_ok=True)
    (base["repo_root"] / "pyproject.toml").write_text("[project]\nname='fake'\n")
    cfg_rel = Path(base["config"])
    (base["repo_root"] / cfg_rel.parent).mkdir(parents=True, exist_ok=True)
    (base["repo_root"] / cfg_rel).write_text("data: {}\n")
    return RemoteRunOptions(**base)


def test_happy_path_runs_all_6_steps_in_order(tmp_path, host):
    provider = FakeProvider(host)
    opts = _opts(tmp_path, provider)

    calls: list[str] = []

    def fake_sync_repo(_host, _repo_root):
        calls.append("sync_repo")

    def fake_ssh_exec(_host, cmd, stream=True):
        # Distinguish bootstrap vs uv sync vs pipeline via the command shape
        if "apt-get" in cmd:
            calls.append("bootstrap")
        elif "uv sync" in cmd:
            calls.append("uv_sync")
        elif "acestor.run" in cmd:
            calls.append("pipeline")
        else:
            calls.append(f"other:{cmd[:20]}")
        return 0

    def fake_sync_back(_host, _run_id, _root):
        calls.append("sync_back")
        return 4242

    with (
        patch("acestor.remote.runner.sync_repo", side_effect=fake_sync_repo),
        patch("acestor.remote.runner.ssh_exec", side_effect=fake_ssh_exec),
        patch("acestor.remote.runner.sync_artifacts_back", side_effect=fake_sync_back),
    ):
        outcome = run_remote(opts)

    assert outcome.exit_status == "ok"
    assert outcome.artifact_bytes == 4242
    assert calls == ["sync_repo", "bootstrap", "uv_sync", "pipeline", "sync_back"]
    assert provider.provisioned == 1
    assert provider.terminated == 1

    row = json.loads((tmp_path / "runs.jsonl").read_text().strip())
    assert row["exit_status"] == "ok"
    assert row["artifact_bytes"] == 4242


def test_pipeline_failure_still_syncs_artifacts_and_terminates(tmp_path, host):
    provider = FakeProvider(host)
    opts = _opts(tmp_path, provider)

    def fake_ssh_exec(_h, cmd, stream=True):
        return 42 if "acestor.run" in cmd else 0

    with (
        patch("acestor.remote.runner.sync_repo"),
        patch("acestor.remote.runner.ssh_exec", side_effect=fake_ssh_exec),
        patch("acestor.remote.runner.sync_artifacts_back", return_value=100) as sab,
    ):
        outcome = run_remote(opts)

    assert outcome.exit_status == "pipeline_failed"
    assert "exited 42" in outcome.error
    assert provider.terminated == 1
    sab.assert_called_once()  # artifacts still fetched on failure


def test_bootstrap_failure_classified_as_sync_failed(tmp_path, host):
    provider = FakeProvider(host)
    opts = _opts(tmp_path, provider)

    def fake_ssh_exec(_h, cmd, stream=True):
        return 1 if "apt-get" in cmd else 0

    with (
        patch("acestor.remote.runner.sync_repo"),
        patch("acestor.remote.runner.ssh_exec", side_effect=fake_ssh_exec),
        patch("acestor.remote.runner.sync_artifacts_back") as sab,
    ):
        outcome = run_remote(opts)

    assert outcome.exit_status == "sync_failed"
    assert "system bootstrap exited 1" in outcome.error
    # Bootstrap failed before pipeline ran → no artifact fetch
    sab.assert_not_called()
    assert provider.terminated == 1


def test_skip_run_bootstraps_but_does_not_execute_pipeline(tmp_path, host):
    provider = FakeProvider(host)
    opts = _opts(tmp_path, provider, skip_run=True)

    exec_cmds: list[str] = []

    def fake_ssh_exec(_h, cmd, stream=True):
        exec_cmds.append(cmd)
        return 0

    with (
        patch("acestor.remote.runner.sync_repo"),
        patch("acestor.remote.runner.ssh_exec", side_effect=fake_ssh_exec),
        patch("acestor.remote.runner.sync_artifacts_back") as sab,
    ):
        outcome = run_remote(opts)

    assert outcome.exit_status == "ok"
    assert not any("acestor.run" in c for c in exec_cmds)
    sab.assert_not_called()
    assert provider.terminated == 1


def test_keep_alive_on_failure_does_not_terminate(tmp_path, host):
    provider = FakeProvider(host)
    opts = _opts(tmp_path, provider, keep_alive_on_failure=True)

    with (
        patch("acestor.remote.runner.sync_repo"),
        patch("acestor.remote.runner.ssh_exec", return_value=99),
        patch("acestor.remote.runner.sync_artifacts_back", return_value=0),
    ):
        outcome = run_remote(opts)

    assert outcome.exit_status == "sync_failed"
    assert provider.terminated == 0  # host left running for debugging


def test_repo_sync_failure_classified_and_host_terminated(tmp_path, host):
    provider = FakeProvider(host)
    opts = _opts(tmp_path, provider)

    with (
        patch("acestor.remote.runner.sync_repo", side_effect=RuntimeError("no disk")),
        patch("acestor.remote.runner.ssh_exec") as exec_mock,
        patch("acestor.remote.runner.sync_artifacts_back") as sab,
    ):
        outcome = run_remote(opts)

    assert outcome.exit_status == "sync_failed"
    assert "no disk" in outcome.error
    exec_mock.assert_not_called()
    sab.assert_not_called()
    assert provider.terminated == 1

"""End-to-end smoke test of the runner wiring via the mock provider."""

from __future__ import annotations

import json
from pathlib import Path

from acestor.remote.providers.mock import MockProvider
from acestor.remote.runner import RemoteRunOptions, run_remote


def test_runner_provisions_terminates_and_writes_ledger(tmp_path: Path) -> None:
    ledger = tmp_path / "runs.jsonl"
    opts = RemoteRunOptions(
        pipeline="pipelines.dengue.pipeline:build_pipeline",
        config="configs/ka_district.yaml",
        run_id="smoke-1",
        provider=MockProvider(),
        instance_type="c7i.2xlarge",
        lifecycle="spot",
        region="ap-south-1",
        ledger_path=ledger,
    )

    outcome = run_remote(opts)

    assert outcome.exit_status == "ok"
    assert outcome.host is not None
    assert outcome.host.id == "i-mock000000"

    row = json.loads(ledger.read_text().splitlines()[0])
    assert row["run_id"] == "smoke-1"
    assert row["provider"] == "mock"
    assert row["exit_status"] == "ok"
    assert row["instance_id"] == "i-mock000000"
    assert row["lifecycle"] == "spot"


def test_runner_records_provision_failure(tmp_path: Path) -> None:
    """If provision raises, the ledger still gets a row with provision_failed."""

    from acestor.remote.providers.base import (
        CloudProvider,
        Lifecycle,
        RemoteHost,
    )

    class BrokenProvider(CloudProvider):
        name = "broken"

        def provision(
            self,
            instance_type: str,
            lifecycle: Lifecycle,
            region: str,
            run_id: str = "",
        ) -> RemoteHost:
            raise RuntimeError("quota exceeded")

        def terminate(self, host: RemoteHost) -> None:  # pragma: no cover
            pass

    ledger = tmp_path / "runs.jsonl"
    opts = RemoteRunOptions(
        pipeline="p:build",
        config="cfg.yaml",
        run_id="fail-1",
        provider=BrokenProvider(),
        instance_type="c7i.2xlarge",
        lifecycle="spot",
        region="ap-south-1",
        ledger_path=ledger,
    )

    outcome = run_remote(opts)

    assert outcome.exit_status == "provision_failed"
    assert "quota exceeded" in outcome.error

    row = json.loads(ledger.read_text().splitlines()[0])
    assert row["exit_status"] == "provision_failed"
    assert row["instance_id"] == ""
    assert "quota exceeded" in row["error"]

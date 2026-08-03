"""Ledger append: correctness + concurrency-safety of the single-line write."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from acestor.remote.ledger import RemoteRunRecord, append_run_record


def _record(run_id: str) -> RemoteRunRecord:
    return RemoteRunRecord(
        run_id=run_id,
        pipeline="pipelines.dengue.pipeline:build_pipeline",
        config="configs/ka_district.yaml",
        provider="mock",
        instance_type="c7i.2xlarge",
        lifecycle="spot",
        region="ap-south-1",
        instance_id=f"i-{run_id}",
        started_at="2026-07-31T00:00:00+00:00",
        ended_at="2026-07-31T00:00:01+00:00",
        wall_seconds=1.0,
        exit_status="ok",
    )


def test_append_creates_parent_dir_and_writes_one_line(tmp_path: Path) -> None:
    ledger = tmp_path / "nested" / "remote_runs.jsonl"
    append_run_record(_record("r1"), path=ledger)

    assert ledger.exists()
    lines = ledger.read_text().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["run_id"] == "r1"
    assert parsed["exit_status"] == "ok"


def test_append_is_append_only(tmp_path: Path) -> None:
    ledger = tmp_path / "remote_runs.jsonl"
    for i in range(3):
        append_run_record(_record(f"r{i}"), path=ledger)

    lines = ledger.read_text().splitlines()
    assert [json.loads(line)["run_id"] for line in lines] == ["r0", "r1", "r2"]


def test_concurrent_appends_do_not_tear_lines(tmp_path: Path) -> None:
    """Under PIPE_BUF, per-line os.write is atomic across processes/threads.

    Records here are ~450 bytes each — well under the 4096-byte guarantee —
    so concurrent writers must produce N intact JSON lines, in some order,
    with no interleaved garbage.
    """
    ledger = tmp_path / "remote_runs.jsonl"
    n = 50

    def write(i: int) -> None:
        append_run_record(_record(f"r{i:02d}"), path=ledger)

    with ThreadPoolExecutor(max_workers=16) as ex:
        list(ex.map(write, range(n)))

    lines = ledger.read_text().splitlines()
    assert len(lines) == n
    ids = sorted(json.loads(line)["run_id"] for line in lines)
    assert ids == sorted(f"r{i:02d}" for i in range(n))
    # Every line parses — no torn rows.
    for line in lines:
        parsed = json.loads(line)
        assert parsed["provider"] == "mock"


def test_record_line_size_is_under_pipe_buf(tmp_path: Path) -> None:
    """Sanity-check the assumption behind the lock-free concurrency test."""
    ledger = tmp_path / "remote_runs.jsonl"
    rec = _record("safety-check")
    rec.extra = {"metadata": "x" * 200}  # generous but still tiny
    append_run_record(rec, path=ledger)
    line = ledger.read_text()
    assert (
        len(line.encode("utf-8")) < 4096
    ), "records must stay under PIPE_BUF for the lock-free append to be safe"
    _ = os.stat(ledger)  # touch stat to keep the import used elsewhere

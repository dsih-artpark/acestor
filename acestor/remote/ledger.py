"""Append-only JSONL ledger of remote runs.

One line per run at ``~/.acestor/remote_runs.jsonl``. Writes are atomic per
line (single ``write()`` of a bytes-terminated JSON string) so concurrent
runners don't tear each other's rows.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

DEFAULT_LEDGER_PATH = Path.home() / ".acestor" / "remote_runs.jsonl"

ExitStatus = Literal[
    "ok",  # pipeline exit 0, artifacts synced
    "pipeline_failed",  # pipeline exit non-zero
    "spot_interrupted",  # AWS reclaimed the instance mid-run
    "provision_failed",  # never got a usable host
    "sync_failed",  # host was fine, transferring inputs or artifacts failed
    "unknown",  # runner crashed before it could classify
]


@dataclass
class RemoteRunRecord:
    run_id: str
    pipeline: str
    config: str
    provider: str
    instance_type: str
    lifecycle: str  # "spot" | "on-demand"
    region: str
    instance_id: str = ""
    started_at: str = ""  # ISO 8601 UTC
    ended_at: str = ""
    wall_seconds: float = 0.0
    exit_status: ExitStatus = "unknown"
    artifact_bytes: int = 0
    interruption: bool = False
    spot_price_usd_per_hr: float | None = None
    error: str = ""  # short one-liner if exit_status != ok
    extra: dict[str, Any] = field(default_factory=dict)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def append_run_record(
    record: RemoteRunRecord, path: Path = DEFAULT_LEDGER_PATH
) -> None:
    """Atomically append one JSON line to the ledger.

    ``os.write`` on a single line-terminated buffer is atomic up to PIPE_BUF
    (4096+ bytes on Linux/macOS) so concurrent runners can share this file
    without a lock as long as one record fits — records here are tiny (<1 KB).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(asdict(record), separators=(",", ":")) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)

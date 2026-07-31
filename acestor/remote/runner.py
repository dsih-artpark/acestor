"""Orchestrates the 6-step remote-run lifecycle.

    provision → bootstrap → sync-up → run → sync-down → terminate

Slice 1 wires provision + terminate + ledger only. Bootstrap / sync / SSH
exec land in later slices — each one drops in behind the same
:class:`RemoteRunOptions` surface so the CLI never grows a new flag per
slice.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from acestor.remote.ledger import (
    DEFAULT_LEDGER_PATH,
    ExitStatus,
    RemoteRunRecord,
    append_run_record,
    utc_now_iso,
)
from acestor.remote.providers.base import CloudProvider, Lifecycle, RemoteHost

log = logging.getLogger(__name__)


@dataclass
class RemoteRunOptions:
    pipeline: str
    config: str
    run_id: str
    provider: CloudProvider
    instance_type: str
    lifecycle: Lifecycle
    region: str
    overrides: list[str] = field(default_factory=list)
    clean: bool = False
    ledger_path: Any = (
        DEFAULT_LEDGER_PATH  # Path — Any keeps the dataclass import clean
    )


@dataclass
class RemoteRunOutcome:
    exit_status: ExitStatus
    wall_seconds: float
    host: RemoteHost | None
    error: str = ""


def run_remote(opts: RemoteRunOptions) -> RemoteRunOutcome:
    """Execute the pipeline on a freshly provisioned remote host.

    Guarantees ``terminate`` is called and one ledger row is appended, no
    matter how the run exits. This is the sole entry point for the CLI.
    """
    started = time.monotonic()
    started_iso = utc_now_iso()

    host: RemoteHost | None = None
    exit_status: ExitStatus = "unknown"
    error = ""

    try:
        log.info(
            "remote runner: provisioning %s (%s) via %s in %s for run_id=%s",
            opts.instance_type,
            opts.lifecycle,
            opts.provider.name,
            opts.region,
            opts.run_id,
        )
        host = opts.provider.provision(
            instance_type=opts.instance_type,
            lifecycle=opts.lifecycle,
            region=opts.region,
        )
        log.info(
            "remote runner: host ready — id=%s ip=%s user=%s",
            host.id,
            host.ip,
            host.ssh_user,
        )

        # Slice 1 stops here — the real bootstrap/sync/exec/download live in
        # later slices. Mark the mock run as OK so ledger + CLI wiring can
        # be exercised without a real cloud round-trip.
        exit_status = "ok"

    except Exception as exc:
        log.exception("remote runner: fatal error before or during run")
        exit_status = "unknown" if host is None else "pipeline_failed"
        if host is None:
            exit_status = "provision_failed"
        error = f"{type(exc).__name__}: {exc}"

    finally:
        if host is not None:
            try:
                opts.provider.terminate(host)
            except Exception:
                log.exception(
                    "remote runner: terminate() raised for host=%s — "
                    "check the cloud console",
                    host.id,
                )

        wall = time.monotonic() - started
        record = RemoteRunRecord(
            run_id=opts.run_id,
            pipeline=opts.pipeline,
            config=opts.config,
            provider=opts.provider.name,
            instance_type=opts.instance_type,
            lifecycle=opts.lifecycle,
            region=opts.region,
            instance_id=host.id if host is not None else "",
            started_at=started_iso,
            ended_at=utc_now_iso(),
            wall_seconds=round(wall, 2),
            exit_status=exit_status,
            error=error,
            extra=dict(host.metadata) if host is not None else {},
        )
        try:
            append_run_record(record, path=opts.ledger_path)
        except Exception:
            log.exception(
                "remote runner: failed to append ledger row — record=%r", record
            )

    return RemoteRunOutcome(
        exit_status=exit_status,
        wall_seconds=round(wall, 2),
        host=host,
        error=error,
    )


def generate_run_id() -> str:
    return uuid.uuid4().hex[:12]

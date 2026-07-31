"""SSH + rsync transport for the remote runner.

Thin wrappers over the system ``ssh`` and ``rsync`` binaries — deliberately
no ``paramiko`` dependency. Two reasons:

1. The system binaries are already required for artifact sync (rsync is not
   in paramiko), so adopting paramiko would mean we still shell out anyway.
2. Streaming stdout/stderr back live from a long-running remote command is
   trivial with ``subprocess.Popen`` and gnarly with paramiko's channel API.

Everything here talks to a :class:`RemoteHost` — no cloud-specific knowledge
leaks past the provider seam.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Iterable

from acestor.remote.providers.base import RemoteHost

log = logging.getLogger(__name__)


# StrictHostKeyChecking=no + UserKnownHostsFile=/dev/null: every remote host
# is fresh and unique to this run — pinning host keys is pointless and just
# fills known_hosts with garbage. LogLevel=ERROR silences the "Warning:
# Permanently added" chatter from the same setting.
_SSH_OPTS = [
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "UserKnownHostsFile=/dev/null",
    "-o",
    "LogLevel=ERROR",
    "-o",
    "ServerAliveInterval=30",
    "-o",
    "ServerAliveCountMax=10",
]


def _ssh_prefix(host: RemoteHost) -> list[str]:
    return [
        "ssh",
        "-i",
        host.ssh_key_path,
        *_SSH_OPTS,
        f"{host.ssh_user}@{host.ip}",
    ]


def _rsync_ssh_arg(host: RemoteHost) -> str:
    """The ``-e`` value for rsync so it uses the same ssh options we do."""
    opts_flat = " ".join(_SSH_OPTS)
    return f"ssh -i {host.ssh_key_path} {opts_flat}"


def ssh_exec(
    host: RemoteHost,
    command: str,
    stream: bool = True,
) -> int:
    """Run ``command`` on ``host`` via ssh, returning the exit code.

    When ``stream`` is True (default) stdout+stderr from the remote are
    interleaved and forwarded to the local process's stdout live — the user
    sees the pipeline logs as they happen. When False, output is captured
    and logged as a single block after completion (useful for short setup
    commands whose output would otherwise clutter the terminal).
    """
    argv = [*_ssh_prefix(host), command]
    log.info("ssh_exec: %s@%s → %s", host.ssh_user, host.ip, _preview(command))

    if not stream:
        proc = subprocess.run(argv, capture_output=True, text=True)
        if proc.stdout:
            log.info("ssh_exec stdout:\n%s", proc.stdout)
        if proc.stderr:
            log.info("ssh_exec stderr:\n%s", proc.stderr)
        return proc.returncode

    # Streamed: merge stderr into stdout so line ordering matches what the
    # remote actually produced. Anything the pipeline logs shows up here.
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,  # line-buffered
    )
    assert proc.stdout is not None  # for the type checker
    try:
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
    finally:
        proc.wait()
    return proc.returncode


def rsync_up(
    host: RemoteHost,
    local_dir: Path | str,
    remote_dir: str,
    exclude: Iterable[str] | None = None,
    delete: bool = True,
    files_from: Path | str | None = None,
) -> None:
    """Push ``local_dir`` → ``host:remote_dir`` via rsync-over-ssh.

    ``delete=True`` means files gone from the local side are removed from
    the remote — mirrors the local tree exactly. Turn it off for input
    datasets you may want to accumulate rather than mirror.
    """
    local = str(local_dir).rstrip("/") + "/"
    remote = f"{host.ssh_user}@{host.ip}:{remote_dir.rstrip('/')}/"
    excludes = [f"--exclude={e}" for e in (exclude or [])]
    files_from_args = ["--files-from", str(files_from), "--from0"] if files_from else []
    # --files-from disables recursive discovery, so keep -a but the list is
    # authoritative. --from0 lets us feed NUL-separated paths so filenames
    # with spaces (e.g. 'dengue-model-pipeline-automation - GBA/') work.
    argv = [
        "rsync",
        "-a",
        "--compress",
        *(["--delete"] if delete else []),
        "--stats",
        "-e",
        _rsync_ssh_arg(host),
        *files_from_args,
        *excludes,
        local,
        remote,
    ]
    log.info(
        "rsync_up: %s → %s (delete=%s, excludes=%s)",
        local,
        remote,
        delete,
        list(exclude) if exclude else [],
    )
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"rsync_up failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    if result.stdout:
        log.info("rsync_up stats:\n%s", result.stdout.strip())


def rsync_down(
    host: RemoteHost,
    remote_dir: str,
    local_dir: Path | str,
    exclude: Iterable[str] | None = None,
) -> None:
    """Pull ``host:remote_dir`` → ``local_dir`` via rsync-over-ssh.

    Used by slice 4 for artifact sync-back. Never uses ``--delete`` — local
    artifacts from previous runs must not be wiped.
    """
    remote = f"{host.ssh_user}@{host.ip}:{remote_dir.rstrip('/')}/"
    local = str(local_dir).rstrip("/") + "/"
    Path(local_dir).mkdir(parents=True, exist_ok=True)
    excludes = [f"--exclude={e}" for e in (exclude or [])]
    argv = [
        "rsync",
        "-a",
        "--compress",
        "--stats",
        "-e",
        _rsync_ssh_arg(host),
        *excludes,
        remote,
        local,
    ]
    log.info("rsync_down: %s → %s", remote, local)
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"rsync_down failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    if result.stdout:
        log.info("rsync_down stats:\n%s", result.stdout.strip())


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _preview(command: str, max_len: int = 120) -> str:
    """Single-line preview of a possibly-multiline shell command for logs."""
    flat = " ".join(command.split())
    return flat if len(flat) <= max_len else flat[: max_len - 1] + "…"


# Note on rsync flags used above:
#   --compress / --stats — supported by both GNU rsync (Linux) and openrsync
#     (macOS 14+, protocol 29 compat). We deliberately avoid --info=stats1
#     which is GNU-only and blew up the slice-4 smoke test on macOS.

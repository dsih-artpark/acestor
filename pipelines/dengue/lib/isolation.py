"""Generic subprocess runner for inference jobs that can't share a process.

Issue #46. The use case is TimesFM: torch loaded in the same process as
xgboost's OpenMP runtime can segfault. The model registry imports xgboost
at module load, so torch inference has to happen in a fresh child process.

Contract — file-based, via a temp directory the parent owns:

* Parent writes
    - ``series.npy``  — ``(n_regions, max_len)`` float32, right-padded
    - ``lengths.npy`` — ``(n_regions,)`` int64 true lengths
    - ``spec.json``   — worker spec dict (horizon, repo_id, revision,
                        cache_dir, max_context, per_core_batch_size,
                        max_regions_per_batch)
* Parent invokes the worker as a fresh ``python -m <module>`` against
  the temp dir.
* Child writes
    - ``output.npy``  — ``(n_regions, horizon)`` float32 point forecast
    - ``status.json`` — ``{"ok": true}`` or ``{"ok": false, "error": "..."}``

The parent kills the child's process group on timeout to ensure stray
threads die with it, drains stdout/stderr for diagnostics, and translates
every failure mode (timeout, missing/!ok status, non-zero exit, missing or
unreadable output, wrong shape, non-finite values) into an
:class:`IsolatedRunError` with a useful message.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)


class IsolatedRunError(RuntimeError):
    """Anything that went wrong inside or around the subprocess."""


@dataclass(frozen=True)
class IsolatedRunResult:
    output: np.ndarray  # (n_regions, horizon) float32
    stdout: str
    stderr: str


def run_isolated(
    *,
    worker_module: str,
    series: np.ndarray,
    lengths: np.ndarray,
    spec: dict[str, Any],
    workdir: Path,
    timeout_s: float,
    expected_shape: tuple[int, int],
) -> IsolatedRunResult:
    """Run ``python -m <worker_module> <workdir>`` and parse its output.

    Caller supplies a fresh ``workdir`` (a temp dir is fine; it is not
    cleaned up here — the caller owns it).
    """
    workdir.mkdir(parents=True, exist_ok=True)
    series_path = workdir / "series.npy"
    lengths_path = workdir / "lengths.npy"
    spec_path = workdir / "spec.json"
    output_path = workdir / "output.npy"
    status_path = workdir / "status.json"

    np.save(series_path, np.asarray(series, dtype=np.float32))
    np.save(lengths_path, np.asarray(lengths, dtype=np.int64))
    spec_path.write_text(json.dumps(spec))

    cmd = [sys.executable, "-m", worker_module, str(workdir)]
    # New session so we can signal the whole group on timeout; this guarantees
    # any threads torch/jax forked inside the child also die with it.
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout_b, stderr_b = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        # Drain output so file handles don't linger; ignore further errors.
        try:
            stdout_b, stderr_b = proc.communicate(timeout=5)
        except Exception:
            stdout_b, stderr_b = b"", b""
        raise IsolatedRunError(
            f"isolation: worker {worker_module!r} exceeded {timeout_s}s and was killed"
        )

    stdout = stdout_b.decode(errors="replace") if stdout_b else ""
    stderr = stderr_b.decode(errors="replace") if stderr_b else ""

    if proc.returncode != 0:
        raise IsolatedRunError(
            f"isolation: worker {worker_module!r} exited with code "
            f"{proc.returncode}. stderr: {stderr.strip()[-500:]}"
        )

    if not status_path.exists():
        raise IsolatedRunError(
            f"isolation: worker {worker_module!r} did not write status.json. "
            f"stderr: {stderr.strip()[-500:]}"
        )
    try:
        status = json.loads(status_path.read_text())
    except json.JSONDecodeError as exc:
        raise IsolatedRunError(
            f"isolation: worker {worker_module!r} wrote unparseable status.json: {exc}"
        ) from exc

    if not status.get("ok"):
        raise IsolatedRunError(
            f"isolation: worker {worker_module!r} reported failure: "
            f"{status.get('error', '<no error message>')}"
        )

    if not output_path.exists():
        raise IsolatedRunError(
            f"isolation: worker {worker_module!r} reported ok but wrote no output.npy"
        )
    try:
        output = np.load(output_path)
    except Exception as exc:
        raise IsolatedRunError(
            f"isolation: worker {worker_module!r} produced an unreadable output.npy: {exc}"
        ) from exc

    if output.shape != expected_shape:
        raise IsolatedRunError(
            f"isolation: worker {worker_module!r} returned shape {output.shape}, "
            f"expected {expected_shape}"
        )
    if not np.all(np.isfinite(output)):
        raise IsolatedRunError(
            f"isolation: worker {worker_module!r} returned non-finite values"
        )

    return IsolatedRunResult(output=output.astype(np.float32), stdout=stdout, stderr=stderr)

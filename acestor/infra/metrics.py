"""Per-run system-resource metrics writer.

A background daemon thread samples CPU / memory / disk every ``interval``
seconds and appends one JSON line per sample to ``<run_dir>/metrics.jsonl``.
The file rides the runner's normal rsync-back path so the caller webui can
render live sparklines next to the log tail.

Rationale — compute boxes run on t3.micro/t3.nano with 512 MB - 1 GB of RAM.
When a step hits memory pressure the log often goes silent well before the
OOM killer arrives, and the operator has no way to tell "hung" from "swapping
hard" from "genuinely progressing on a heavy pandas op." This writer surfaces
the exact ramp so they don't have to guess.

Design constraints:
- Daemon thread — dies with the interpreter, no join needed on crash.
- Fail-open — if psutil is missing (dev machine without the extras) or a
  sample raises for any reason, the writer logs once and shuts up. The
  pipeline itself never fails because of metrics.
- Tiny footprint — one flush per sample, no buffering, no rotation. Metrics
  file for a 30-min run at 5s cadence = 360 lines ≈ 40 KB. Rsync-back cost
  is negligible.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

_METRICS_FILENAME = "metrics.jsonl"


class MetricsWriter:
    """Sample /proc-style system metrics into ``<run_dir>/metrics.jsonl``."""

    def __init__(self, run_dir: Path, interval: float = 5.0) -> None:
        self.run_dir = Path(run_dir)
        self.interval = max(1.0, float(interval))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Kick off the sampler thread. Idempotent — no-op if already running
        or if psutil isn't importable in this environment."""
        if self._thread is not None:
            return
        try:
            import psutil  # noqa: F401, PLC0415
        except ImportError:
            log.info(
                "metrics: psutil not installed — skipping resource sampling "
                "(install psutil to enable the webui metrics strip)"
            )
            return
        try:
            self.run_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("metrics: could not create run_dir %s: %s", self.run_dir, exc)
            return
        self._thread = threading.Thread(
            target=self._loop, name="acestor-metrics", daemon=True
        )
        self._thread.start()
        log.info(
            "metrics: writer started (interval=%.1fs, path=%s/%s)",
            self.interval,
            self.run_dir,
            _METRICS_FILENAME,
        )

    def stop(self, timeout: float = 2.0) -> None:
        """Signal the sampler to stop and wait briefly for it to flush."""
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=timeout)
        self._thread = None

    def _loop(self) -> None:
        import psutil  # noqa: PLC0415

        path = self.run_dir / _METRICS_FILENAME
        # cpu_percent needs a first "priming" call — the first sample is
        # meaningless if we don't warm it up. See psutil docs.
        try:
            psutil.cpu_percent(interval=None)
        except Exception:  # noqa: BLE001
            pass

        while not self._stop.is_set():
            sample = _sample(psutil)
            if sample is not None:
                try:
                    with open(path, "a") as f:
                        f.write(json.dumps(sample) + "\n")
                except OSError as exc:
                    # Disk full / permission change mid-run: give up quietly
                    # rather than spam the log.
                    log.debug("metrics: write failed (%s) — stopping writer", exc)
                    return
            # Sleep in small slices so stop() responds within ~0.5s even at
            # interval=60.
            deadline = time.monotonic() + self.interval
            while not self._stop.is_set() and time.monotonic() < deadline:
                time.sleep(min(0.5, deadline - time.monotonic()))


def _sample(psutil_mod) -> dict | None:
    """Take one snapshot. Returns None on any failure so the loop keeps going."""
    try:
        vm = psutil_mod.virtual_memory()
        du = psutil_mod.disk_usage("/")
        return {
            "ts": time.time(),
            "cpu_pct": psutil_mod.cpu_percent(interval=None),
            "mem_pct": vm.percent,
            "mem_used_mb": int(vm.used / (1024 * 1024)),
            "mem_total_mb": int(vm.total / (1024 * 1024)),
            "disk_pct": du.percent,
            "disk_used_gb": round(du.used / (1024**3), 2),
            "disk_total_gb": round(du.total / (1024**3), 2),
        }
    except Exception as exc:  # noqa: BLE001
        log.debug("metrics: sample failed (%s)", exc)
        return None

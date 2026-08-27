"""Tests for the per-run system-metrics writer."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from acestor.infra.metrics import MetricsWriter, _sample


def test_metrics_writer_writes_samples(tmp_path: Path) -> None:
    """Happy path — writer starts, samples for a beat, stop() flushes."""
    pytest.importorskip("psutil")
    writer = MetricsWriter(tmp_path, interval=0.5)
    writer.start()
    # Give it enough wall time to write at least 2 samples.
    time.sleep(1.3)
    writer.stop()

    metrics_file = tmp_path / "metrics.jsonl"
    assert metrics_file.is_file(), "writer should have created metrics.jsonl"
    lines = [line for line in metrics_file.read_text().splitlines() if line.strip()]
    assert len(lines) >= 2, f"expected >=2 samples, got {len(lines)}"

    # Every line is valid JSON with the documented shape.
    for line in lines:
        d = json.loads(line)
        assert set(d) >= {
            "ts",
            "cpu_pct",
            "mem_pct",
            "mem_used_mb",
            "mem_total_mb",
            "disk_pct",
            "disk_used_gb",
            "disk_total_gb",
        }
        assert 0 <= d["mem_pct"] <= 100
        assert 0 <= d["disk_pct"] <= 100
        assert d["mem_used_mb"] >= 0
        assert d["mem_total_mb"] > 0


def test_metrics_writer_stop_is_idempotent(tmp_path: Path) -> None:
    pytest.importorskip("psutil")
    writer = MetricsWriter(tmp_path, interval=0.5)
    writer.start()
    writer.stop()
    # Second stop must not raise.
    writer.stop()


def test_metrics_writer_start_is_idempotent(tmp_path: Path) -> None:
    """Second start() must be a no-op — otherwise you leak sampler threads."""
    pytest.importorskip("psutil")
    writer = MetricsWriter(tmp_path, interval=0.5)
    writer.start()
    thread_after_first = writer._thread
    writer.start()  # noqa — testing idempotency
    assert writer._thread is thread_after_first
    writer.stop()


def test_metrics_writer_missing_psutil_is_silent(tmp_path: Path, monkeypatch) -> None:
    """If psutil isn't importable, start() must log-and-return, not crash."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "psutil":
            raise ImportError("simulated: psutil not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    writer = MetricsWriter(tmp_path, interval=0.5)
    writer.start()
    # No thread, no file — pipeline must run happily even without psutil.
    assert writer._thread is None
    assert not (tmp_path / "metrics.jsonl").exists()
    writer.stop()  # also must not raise


def test_sample_returns_expected_shape() -> None:
    """Sanity — one raw sample has every documented field with plausible ranges."""
    psutil = pytest.importorskip("psutil")
    s = _sample(psutil)
    assert s is not None
    assert 0 <= s["cpu_pct"] <= 100
    assert 0 <= s["mem_pct"] <= 100
    assert s["mem_total_mb"] > s["mem_used_mb"] >= 0
    assert s["disk_total_gb"] > 0

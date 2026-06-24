"""Tests for the isolation subprocess runner (#46).

Uses a tiny stub worker module written into a temp tests dir and added
to ``sys.path`` so we can exercise the runner against every failure
mode without dragging in timesfm/torch.
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path

import numpy as np
import pytest

from pipelines.dengue.lib.isolation import IsolatedRunError, run_isolated


# ---------------------------------------------------------------------------
# Stub-worker installation
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_workers(tmp_path: Path, monkeypatch) -> str:
    """Install a package of stub worker modules; return its parent path."""
    pkg = tmp_path / "_stubs"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")

    # Each worker is a self-contained module; the runner invokes them as
    # ``python -m _stubs.<name> <workdir>``.
    workers = {
        "ok": """
            import json, sys, numpy as np
            from pathlib import Path
            wd = Path(sys.argv[1])
            spec = json.loads((wd/"spec.json").read_text())
            horizon = int(spec["horizon"])
            series = np.load(wd/"series.npy")
            n = series.shape[0]
            # Mirror the trailing value so the output is data-derived.
            out = np.tile(series[:, -1:], (1, horizon)).astype(np.float32)
            np.save(wd/"output.npy", out)
            (wd/"status.json").write_text(json.dumps({"ok": True}))
        """,
        "exc": """
            import json, sys
            from pathlib import Path
            wd = Path(sys.argv[1])
            (wd/"status.json").write_text(json.dumps({"ok": False, "error": "boom"}))
            sys.exit(1)
        """,
        "noout": """
            import json, sys
            from pathlib import Path
            wd = Path(sys.argv[1])
            (wd/"status.json").write_text(json.dumps({"ok": True}))
        """,
        "wrongshape": """
            import json, sys, numpy as np
            from pathlib import Path
            wd = Path(sys.argv[1])
            np.save(wd/"output.npy", np.zeros((1, 1), dtype=np.float32))
            (wd/"status.json").write_text(json.dumps({"ok": True}))
        """,
        "nonfinite": """
            import json, sys, numpy as np
            from pathlib import Path
            wd = Path(sys.argv[1])
            spec = json.loads((wd/"spec.json").read_text())
            horizon = int(spec["horizon"])
            n = int(np.load(wd/"series.npy").shape[0])
            out = np.full((n, horizon), np.nan, dtype=np.float32)
            np.save(wd/"output.npy", out)
            (wd/"status.json").write_text(json.dumps({"ok": True}))
        """,
        "nostatus": """
            import sys
            sys.exit(0)
        """,
        "nonzero": """
            import sys
            sys.exit(7)
        """,
        "hang": """
            import time
            time.sleep(30)
        """,
    }
    for name, src in workers.items():
        (pkg / f"{name}.py").write_text(textwrap.dedent(src))

    sys.path.insert(0, str(tmp_path))
    # The subprocess won't see sys.path edits; PYTHONPATH propagates via env.
    existing = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv(
        "PYTHONPATH",
        f"{tmp_path}{os.pathsep}{existing}" if existing else str(tmp_path),
    )
    try:
        yield "_stubs"
    finally:
        sys.path.remove(str(tmp_path))
        # Drop cached imports so different tests don't share state
        for mod in list(sys.modules):
            if mod.startswith("_stubs"):
                del sys.modules[mod]


def _spec(horizon: int = 2) -> dict:
    return {
        "horizon": horizon,
        "repo_id": "stub",
        "revision": "a" * 40,
        "cache_dir": "/tmp/stub",
        "max_context": 64,
        "per_core_batch_size": 8,
        "max_regions_per_batch": 4,
    }


def _series(n: int = 2, length: int = 8) -> tuple[np.ndarray, np.ndarray]:
    series = np.arange(n * length, dtype=np.float32).reshape(n, length)
    lengths = np.full(n, length, dtype=np.int64)
    return series, lengths


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_runner_returns_output_on_success(tmp_path: Path, stub_workers: str):
    series, lengths = _series(n=3, length=10)
    result = run_isolated(
        worker_module=f"{stub_workers}.ok",
        series=series,
        lengths=lengths,
        spec=_spec(horizon=2),
        workdir=tmp_path / "wd",
        timeout_s=10,
        expected_shape=(3, 2),
    )
    assert result.output.shape == (3, 2)
    # Stub mirrored the last column.
    np.testing.assert_array_equal(result.output, np.tile(series[:, -1:], (1, 2)))


def test_runner_raises_on_worker_status_failure(tmp_path: Path, stub_workers: str):
    series, lengths = _series()
    with pytest.raises(IsolatedRunError, match="exited with code"):
        run_isolated(
            worker_module=f"{stub_workers}.exc",
            series=series,
            lengths=lengths,
            spec=_spec(),
            workdir=tmp_path / "wd",
            timeout_s=10,
            expected_shape=(2, 2),
        )


def test_runner_raises_on_missing_output(tmp_path: Path, stub_workers: str):
    series, lengths = _series()
    with pytest.raises(IsolatedRunError, match="no output.npy"):
        run_isolated(
            worker_module=f"{stub_workers}.noout",
            series=series,
            lengths=lengths,
            spec=_spec(),
            workdir=tmp_path / "wd",
            timeout_s=10,
            expected_shape=(2, 2),
        )


def test_runner_raises_on_wrong_shape(tmp_path: Path, stub_workers: str):
    series, lengths = _series()
    with pytest.raises(IsolatedRunError, match="returned shape"):
        run_isolated(
            worker_module=f"{stub_workers}.wrongshape",
            series=series,
            lengths=lengths,
            spec=_spec(),
            workdir=tmp_path / "wd",
            timeout_s=10,
            expected_shape=(2, 2),
        )


def test_runner_raises_on_nonfinite(tmp_path: Path, stub_workers: str):
    series, lengths = _series()
    with pytest.raises(IsolatedRunError, match="non-finite"):
        run_isolated(
            worker_module=f"{stub_workers}.nonfinite",
            series=series,
            lengths=lengths,
            spec=_spec(horizon=2),
            workdir=tmp_path / "wd",
            timeout_s=10,
            expected_shape=(2, 2),
        )


def test_runner_raises_on_missing_status(tmp_path: Path, stub_workers: str):
    series, lengths = _series()
    with pytest.raises(IsolatedRunError, match="did not write status.json"):
        run_isolated(
            worker_module=f"{stub_workers}.nostatus",
            series=series,
            lengths=lengths,
            spec=_spec(),
            workdir=tmp_path / "wd",
            timeout_s=10,
            expected_shape=(2, 2),
        )


def test_runner_raises_on_nonzero_exit(tmp_path: Path, stub_workers: str):
    series, lengths = _series()
    with pytest.raises(IsolatedRunError, match="exited with code 7"):
        run_isolated(
            worker_module=f"{stub_workers}.nonzero",
            series=series,
            lengths=lengths,
            spec=_spec(),
            workdir=tmp_path / "wd",
            timeout_s=10,
            expected_shape=(2, 2),
        )


def test_runner_kills_child_on_timeout(tmp_path: Path, stub_workers: str):
    series, lengths = _series()
    with pytest.raises(IsolatedRunError, match="exceeded .* and was killed"):
        run_isolated(
            worker_module=f"{stub_workers}.hang",
            series=series,
            lengths=lengths,
            spec=_spec(),
            workdir=tmp_path / "wd",
            timeout_s=1,
            expected_shape=(2, 2),
        )

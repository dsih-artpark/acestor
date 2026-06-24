"""TimesFM 2.5 subprocess worker — issue #46.

Imports ONLY ``timesfm`` + ``numpy`` (never the acestor model registry,
which would pull xgboost's OpenMP runtime into the same process). The
parent (:mod:`pipelines.dengue.lib.isolation`) hands work in via files
in a temp directory; this script writes the forecast and a status
record back.

Pins thread counts before importing torch/timesfm so inference is
functionally deterministic within a pinned env (the spec's bar — not
byte-identical across machines).
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path


def _pin_threads() -> None:
    """Pin BLAS/torch thread counts; must run BEFORE any torch import."""
    for var in (
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
    ):
        os.environ.setdefault(var, "1")


def _run(workdir: Path) -> None:
    spec = json.loads((workdir / "spec.json").read_text())

    import numpy as np  # noqa: PLC0415

    series_padded = np.load(workdir / "series.npy")  # (n, max_len) float32
    lengths = np.load(workdir / "lengths.npy")  # (n,) int64
    horizon = int(spec["horizon"])
    n_regions = int(series_padded.shape[0])

    # Trim each series back to its true length — the parent right-pads so the
    # 2D layout transports cleanly, but TimesFM accepts variable-length inputs.
    inputs: list[np.ndarray] = [
        series_padded[i, : int(lengths[i])].astype(np.float32)
        for i in range(n_regions)
    ]

    import timesfm  # noqa: PLC0415

    model = timesfm.TimesFM_2p5_200M_torch.from_pretrained(
        spec["repo_id"],
        revision=spec["revision"],
        cache_dir=spec["cache_dir"],
        local_files_only=True,
    )
    model.compile(
        timesfm.ForecastConfig(
            max_context=int(spec["max_context"]),
            max_horizon=int(horizon),
            normalize_inputs=True,
            infer_is_positive=True,
            per_core_batch_size=int(spec["per_core_batch_size"]),
        )
    )

    # Chunk the regions if the caller asked us to — keeps the per-call batch
    # small for memory predictability. Default = one call.
    chunk = int(spec.get("max_regions_per_batch") or n_regions)
    out_chunks: list[np.ndarray] = []
    for start in range(0, n_regions, chunk):
        sub = inputs[start : start + chunk]
        point, _quantile = model.forecast(horizon=horizon, inputs=sub)
        point = np.asarray(point, dtype=np.float32)
        if point.shape != (len(sub), horizon):
            raise RuntimeError(
                f"timesfm.forecast returned shape {point.shape}, "
                f"expected ({len(sub)}, {horizon})"
            )
        out_chunks.append(point)

    output = np.concatenate(out_chunks, axis=0) if out_chunks else np.zeros(
        (0, horizon), dtype=np.float32
    )
    np.save(workdir / "output.npy", output)


def main() -> int:
    if len(sys.argv) != 2:
        sys.stderr.write("usage: _timesfm_worker.py <workdir>\n")
        return 2

    workdir = Path(sys.argv[1])
    _pin_threads()
    status_path = workdir / "status.json"
    try:
        _run(workdir)
    except Exception:  # broad on purpose — any failure is reported via status.json
        err = traceback.format_exc()
        sys.stderr.write(err)
        try:
            status_path.write_text(
                json.dumps({"ok": False, "error": err.splitlines()[-1][:500]})
            )
        except Exception:
            pass
        return 1
    try:
        status_path.write_text(json.dumps({"ok": True}))
    except Exception:
        # If we can't write status, the parent will report missing-status.
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

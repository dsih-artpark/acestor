"""Tests for the TimesFM 2.5 model (#46).

No torch, no download. The isolated runner is mocked via the ``_runner``
seam on :func:`pipelines.dengue.lib.models.timesfm.forecast`; the
package API contract is pinned in a separate test by stubbing
``timesfm`` in ``sys.modules`` and exercising the worker module
directly in-process.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from pipelines.dengue.lib.models import ModelContext, get_model
from pipelines.dengue.lib.models.timesfm import (
    TimesFmParams,
    build_weekly_series,
    forecast,
    horizon_and_indices_for_targets,
)
from pipelines.dengue.lib.maps import MODEL_FULL_NAME


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


VALID_SHA = "a" * 40


def _params(**over: Any) -> TimesFmParams:
    base = {
        "huggingface_repo_id": "google/timesfm-2.5-200m-pytorch",
        "revision": VALID_SHA,
        "cache_dir": "/tmp/timesfm-cache",
        "max_context": 1024,
        "per_core_batch_size": 32,
        "min_context_weeks": 4,
        "timeout_s": 60,
        "max_regions_per_batch": 128,
    }
    base.update(over)
    return TimesFmParams.from_raw(base)


def _case_df(
    regions=("r1", "r2"), n_weeks: int = 12, base: pd.Timestamp = None
) -> pd.DataFrame:
    base = base or pd.Timestamp("2026-01-05")  # Monday
    rows = []
    for region in regions:
        for w in range(n_weeks):
            rows.append(
                {
                    "recordDate": base + pd.Timedelta(days=7 * w),
                    "district": region,
                    "case": float(w + (10 if region == "r2" else 0)),
                }
            )
    return pd.DataFrame(rows)


def _fake_runner(point_values: np.ndarray):
    """Build a runner stub that returns ``point_values`` (n_regions, horizon)."""
    from pipelines.dengue.lib.isolation import IsolatedRunResult

    def runner(
        *, worker_module, series, lengths, spec, workdir, timeout_s, expected_shape
    ):
        assert (
            point_values.shape == expected_shape
        ), f"test bug: point_values {point_values.shape} != expected {expected_shape}"
        return IsolatedRunResult(
            output=point_values.astype(np.float32), stdout="", stderr=""
        )

    return runner


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def test_params_accepts_full_sha_revision():
    p = _params(revision="0" * 40)
    assert p.revision == "0" * 40


def test_params_rejects_branch_or_tag_as_revision():
    with pytest.raises(ValueError, match="full 40-hex"):
        _params(revision="main")


def test_params_rejects_short_sha():
    with pytest.raises(ValueError, match="full 40-hex"):
        _params(revision="a" * 12)


def test_params_empty_dict_applies_all_defaults():
    p = TimesFmParams.from_raw({})
    assert p.huggingface_repo_id == "google/timesfm-2.5-200m-pytorch"
    assert p.cache_dir == ".cache/timesfm"
    assert p.max_context == 1024
    assert p.per_core_batch_size == 32
    assert p.min_context_weeks == 52
    assert p.timeout_s == 300.0
    assert p.max_regions_per_batch == 128
    # revision default is the pinned SHA (or TIMESFM_REVISION env); either way,
    # must satisfy the 40-hex validator.
    assert len(p.revision) == 40 and all(c in "0123456789abcdef" for c in p.revision)


def test_params_revision_overridable_via_env(monkeypatch):
    monkeypatch.setenv("TIMESFM_REVISION", "b" * 40)
    p = TimesFmParams.from_raw({})
    assert p.revision == "b" * 40


def test_params_yaml_overrides_win_over_env_and_defaults(monkeypatch):
    monkeypatch.setenv("TIMESFM_REVISION", "b" * 40)
    p = TimesFmParams.from_raw({"revision": "c" * 40, "min_context_weeks": 26})
    assert p.revision == "c" * 40
    assert p.min_context_weeks == 26


def test_params_rejects_non_positive_numeric():
    with pytest.raises(ValueError, match="min_context_weeks"):
        _params(min_context_weeks=0)


# ---------------------------------------------------------------------------
# Series construction + horizon math
# ---------------------------------------------------------------------------


def test_build_weekly_series_zero_fills_gaps():
    df = pd.DataFrame(
        {
            "recordDate": ["2026-01-05", "2026-01-26"],  # 3-week gap
            "district": ["r1", "r1"],
            "case": [10.0, 5.0],
        }
    )
    series_by_region = build_weekly_series(
        df, spatial_col="district", cutoff_case=pd.Timestamp("2026-01-26")
    )
    s = series_by_region["r1"]
    # Span = 4 weekly points, 2 zero-filled.
    assert len(s) == 4
    assert (s == [10.0, 0.0, 0.0, 5.0]).all()


def test_build_weekly_series_rejects_off_cadence():
    df = pd.DataFrame(
        {
            "recordDate": ["2026-01-05", "2026-01-08"],  # 3 days apart, not 7
            "district": ["r1", "r1"],
            "case": [1.0, 2.0],
        }
    )
    with pytest.raises(ValueError, match="off-weekly-cadence"):
        build_weekly_series(
            df, spatial_col="district", cutoff_case=pd.Timestamp("2026-01-26")
        )


def test_horizon_index_math_for_4_weekly_targets():
    cutoff = pd.Timestamp("2026-01-05")
    targets = ["2026-01-12", "2026-01-19", "2026-01-26", "2026-02-02"]
    horizon, indices, ts = horizon_and_indices_for_targets(
        cutoff_case=cutoff, prediction_dates=targets
    )
    assert horizon == 4
    assert indices == [0, 1, 2, 3]
    assert ts[0] == pd.Timestamp("2026-01-12")


def test_horizon_empty_prediction_dates_collapses():
    horizon, indices, ts = horizon_and_indices_for_targets(
        cutoff_case=pd.Timestamp("2026-01-05"), prediction_dates=[]
    )
    assert horizon == 0
    assert indices == [] and ts == []


def test_horizon_rejects_off_grid_target():
    with pytest.raises(ValueError, match="off the weekly grid"):
        horizon_and_indices_for_targets(
            cutoff_case=pd.Timestamp("2026-01-05"),
            prediction_dates=["2026-01-09"],
        )


def test_horizon_rejects_non_positive_offset():
    with pytest.raises(ValueError, match="not strictly after"):
        horizon_and_indices_for_targets(
            cutoff_case=pd.Timestamp("2026-01-05"),
            prediction_dates=["2026-01-05"],
        )


# ---------------------------------------------------------------------------
# End-to-end forecast() with a mocked runner
# ---------------------------------------------------------------------------


def test_forecast_returns_contract_columns_and_full_model_name():
    df = _case_df(regions=("r1", "r2"), n_weeks=8)
    cutoff = pd.Timestamp("2026-02-23")  # past the last weekly date
    # Anchor every series at cutoff so spec's weekly grid assumption holds.
    # Re-stamp the synthetic data so its last row IS cutoff.
    df["recordDate"] = pd.to_datetime(df["recordDate"])
    df["recordDate"] = df["recordDate"] + (cutoff - df["recordDate"].max())
    prediction_dates = [str((cutoff + pd.Timedelta(days=7 * h)).date()) for h in (1, 2)]
    point = np.array([[3.0, 4.0], [30.0, 40.0]], dtype=np.float32)
    out = forecast(
        case_df=df,
        spatial_col="district",
        cutoff_case=cutoff,
        prediction_dates=prediction_dates,
        params=_params(min_context_weeks=2),
        _runner=_fake_runner(point),
    )
    assert set(["district", "recordDate", "prediction", "model"]) <= set(out.columns)
    assert (out["model"] == "timesFoundationModel").all()
    assert MODEL_FULL_NAME["timesfm"] == "timesFoundationModel"
    # 2 regions × 2 horizons.
    assert len(out) == 4


def test_forecast_is_spatial_res_agnostic():
    """TimesFM accepts any spatial_res — sparsity is handled by
    ``min_context_weeks`` skipping per-region, not by a hard policy gate."""
    # Stamp 12 weeks of history at the cutoff for a 'ward' region.
    df = _case_df(regions=("ward_1",), n_weeks=12).rename(columns={"district": "ward"})
    cutoff = pd.Timestamp("2026-02-23")
    df["recordDate"] = pd.to_datetime(df["recordDate"]) + (
        cutoff - df["recordDate"].max()
    )
    prediction_dates = [str((cutoff + pd.Timedelta(days=7)).date())]

    point = np.array([[1.0]], dtype=np.float32)
    out = forecast(
        case_df=df,
        spatial_col="ward",
        cutoff_case=cutoff,
        prediction_dates=prediction_dates,
        params=_params(min_context_weeks=2),
        _runner=_fake_runner(point),
    )
    assert "ward" in out.columns
    assert len(out) == 1


def test_forecast_empty_prediction_dates_returns_empty():
    df = _case_df()
    out = forecast(
        case_df=df,
        spatial_col="district",
        cutoff_case=pd.Timestamp("2026-02-23"),
        prediction_dates=[],
        params=_params(min_context_weeks=2),
        _runner=_fake_runner(np.zeros((0, 0), dtype=np.float32)),
    )
    assert out.empty


def test_forecast_all_zero_history_skips_model_call():
    """An all-zero region must forecast 0 directly, not be sent to the model."""
    df = _case_df(regions=("r1",), n_weeks=8)
    df["case"] = 0.0
    cutoff = pd.Timestamp("2026-02-23")
    df["recordDate"] = pd.to_datetime(df["recordDate"])
    df["recordDate"] = df["recordDate"] + (cutoff - df["recordDate"].max())
    runner_called = []

    def spy_runner(**kwargs):
        runner_called.append(kwargs)
        raise AssertionError("runner must not be called for all-zero histories")

    out = forecast(
        case_df=df,
        spatial_col="district",
        cutoff_case=cutoff,
        prediction_dates=[str((cutoff + pd.Timedelta(days=7)).date())],
        params=_params(min_context_weeks=2),
        _runner=spy_runner,
    )
    assert runner_called == []
    assert (out["prediction"] == 0.0).all()
    assert len(out) == 1


def test_forecast_all_regions_skipped_fails_loudly():
    """min_context_weeks > available history with no all-zero regions → raise."""
    df = _case_df(regions=("r1",), n_weeks=4)
    cutoff = pd.Timestamp("2026-02-23")
    df["recordDate"] = pd.to_datetime(df["recordDate"])
    df["recordDate"] = df["recordDate"] + (cutoff - df["recordDate"].max())
    with pytest.raises(RuntimeError, match="min_context_weeks=52"):
        forecast(
            case_df=df,
            spatial_col="district",
            cutoff_case=cutoff,
            prediction_dates=[str((cutoff + pd.Timedelta(days=7)).date())],
            params=_params(min_context_weeks=52),
            _runner=_fake_runner(np.zeros((0, 1), dtype=np.float32)),
        )


# ---------------------------------------------------------------------------
# Registry + ModelContext integration
# ---------------------------------------------------------------------------


def test_model_registered_under_short_name():
    assert get_model("timesfm").__class__.__name__ == "TimesFmModel"


def test_modelcontext_threshold_to_date_equals_cutoff_case():
    ctx = ModelContext(
        merged_df=pd.DataFrame(),
        case_df=pd.DataFrame(),
        cfg=types.SimpleNamespace(spatial_res="district"),
        pred_upto=pd.Timestamp("2026-03-23"),
        cutoff_case=pd.Timestamp("2026-02-23"),
    )
    assert get_model("timesfm").threshold_to_date(ctx) == pd.Timestamp("2026-02-23")


# ---------------------------------------------------------------------------
# Package API contract test — pin the timesfm 2.5 surface
# ---------------------------------------------------------------------------


def test_worker_calls_timesfm_2p5_api_correctly(tmp_path: Path, monkeypatch):
    """Stub ``timesfm`` in sys.modules and run the worker module directly.

    Pins: ``from_pretrained(repo_id, revision, cache_dir, local_files_only=True)``,
    ``compile(ForecastConfig(...))``, and ``forecast(horizon=H, inputs=[...])``
    with no frequency argument. Any drift in the 2.5 surface breaks this test.
    """
    # Build a fake timesfm module.
    fake = types.ModuleType("timesfm")

    captured: dict[str, Any] = {}

    class _ForecastConfig:
        def __init__(self, **kwargs):
            captured["forecast_config"] = kwargs

    class _Model:
        def __init__(self):
            captured.setdefault("model_instances", 0)
            captured["model_instances"] += 1

        def compile(self, cfg):
            captured["compile_called_with"] = cfg

        def forecast(self, horizon, inputs):
            captured["forecast_call"] = {
                "horizon": horizon,
                "n_inputs": len(inputs),
                "input_lengths": [len(x) for x in inputs],
            }
            n = len(inputs)
            point = np.zeros((n, horizon), dtype=np.float32)
            return point, None

    class _Loader:
        @classmethod
        def from_pretrained(cls, repo_id, *, revision, cache_dir, local_files_only):
            captured["from_pretrained"] = {
                "repo_id": repo_id,
                "revision": revision,
                "cache_dir": cache_dir,
                "local_files_only": local_files_only,
            }
            return _Model()

    fake.TimesFM_2p5_200M_torch = _Loader
    fake.ForecastConfig = _ForecastConfig
    monkeypatch.setitem(sys.modules, "timesfm", fake)

    # Worker now calls huggingface_hub.snapshot_download for idempotent
    # cache warming. Stub it so the test stays offline; assert it's called.
    fake_hf = types.ModuleType("huggingface_hub")

    def _snapshot_download(*, repo_id, revision, cache_dir):
        captured["snapshot_download"] = {
            "repo_id": repo_id,
            "revision": revision,
            "cache_dir": cache_dir,
        }
        return cache_dir

    fake_hf.snapshot_download = _snapshot_download
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_hf)

    # Lay out worker inputs.
    workdir = tmp_path / "wd"
    workdir.mkdir()
    # Two variable-length series (the worker trims to true lengths).
    series_padded = np.array(
        [
            [1, 2, 3, 4, 5, 0, 0],
            [10, 20, 30, 0, 0, 0, 0],
        ],
        dtype=np.float32,
    )
    lengths = np.array([5, 3], dtype=np.int64)
    np.save(workdir / "series.npy", series_padded)
    np.save(workdir / "lengths.npy", lengths)
    spec = {
        "horizon": 4,
        "repo_id": "google/timesfm-2.5-200m-pytorch",
        "revision": VALID_SHA,
        "cache_dir": str(tmp_path / "cache"),
        "max_context": 1024,
        "per_core_batch_size": 32,
        "max_regions_per_batch": 128,
    }
    (workdir / "spec.json").write_text(json.dumps(spec))

    # Run the worker in-process — it should not crash and should hit the API.
    from pipelines.dengue.lib import _timesfm_worker

    sys.argv = ["worker", str(workdir)]
    rc = _timesfm_worker.main()

    assert rc == 0
    status = json.loads((workdir / "status.json").read_text())
    assert status == {"ok": True}
    output = np.load(workdir / "output.npy")
    assert output.shape == (2, 4)

    # Pinned API surface:
    assert captured["from_pretrained"] == {
        "repo_id": "google/timesfm-2.5-200m-pytorch",
        "revision": VALID_SHA,
        "cache_dir": str(tmp_path / "cache"),
        "local_files_only": True,
    }
    # snapshot_download was called with the same (repo_id, revision, cache_dir)
    # before from_pretrained, ensuring idempotent cache warming.
    assert captured["snapshot_download"] == {
        "repo_id": "google/timesfm-2.5-200m-pytorch",
        "revision": VALID_SHA,
        "cache_dir": str(tmp_path / "cache"),
    }
    fc = captured["forecast_config"]
    assert fc["max_context"] == 1024
    assert fc["max_horizon"] == 4
    assert fc["normalize_inputs"] is True
    assert fc["infer_is_positive"] is True
    assert fc["per_core_batch_size"] == 32
    # No frequency arg on .forecast.
    assert captured["forecast_call"]["horizon"] == 4
    assert captured["forecast_call"]["n_inputs"] == 2
    # Trimmed to true lengths.
    assert captured["forecast_call"]["input_lengths"] == [5, 3]

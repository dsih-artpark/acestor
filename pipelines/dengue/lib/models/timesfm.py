"""TimesFM 2.5-200m forecasting model (issue #46).

A pretrained univariate time-series foundation model. **Uses only each
region's weekly case history; ignores weather** (unlike nbr/rf/xgb).
Runs in an isolated subprocess via :mod:`pipelines.dengue.lib.isolation`
so torch's runtime can't share a process with xgboost's OpenMP.

Spatial-agnostic — works at any ``spatial_res`` the rest of the pipeline
supports (corp, zone, ward, district, subdistrict, mandal). At
high-resolution units case counts are sparser, so ``min_context_weeks``
and the regular per-region skip logic do the right thing without a
hard policy gate.

Standalone, backtest-gated: integration into the scheduled ensemble
happens after a separate validation gate.

Settings live under ``model_configs.timesfm`` in YAML and are threaded
onto :class:`pipelines.dengue.lib.models.ModelContext.model_params` by
``train_and_predict``:

.. code-block:: yaml

    model:
      models: [timesfm]
      spatial_res: district           # any spatial_res the pipeline supports
      ensemble: none
      output: per_model
    model_configs:
      timesfm:
        huggingface_repo_id: google/timesfm-2.5-200m-pytorch
        revision: "<full 40-hex commit SHA>"
        cache_dir: <gitignored project-local path>
        max_context: 1024
        per_core_batch_size: 32
        min_context_weeks: 52
        timeout_s: 300
        max_regions_per_batch: 128
"""

from __future__ import annotations

import logging
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from pipelines.dengue.lib.isolation import IsolatedRunError, run_isolated

log = logging.getLogger(__name__)


_WORKER_MODULE = "pipelines.dengue.lib._timesfm_worker"
_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")


# ---------------------------------------------------------------------------
# Typed config (validates the raw dict from ``model_configs.timesfm``)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TimesFmParams:
    huggingface_repo_id: str
    revision: str  # full 40-hex SHA
    cache_dir: str
    max_context: int
    per_core_batch_size: int
    min_context_weeks: int
    timeout_s: float
    max_regions_per_batch: int

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> TimesFmParams:
        required = (
            "huggingface_repo_id",
            "revision",
            "cache_dir",
            "max_context",
            "per_core_batch_size",
            "min_context_weeks",
            "timeout_s",
            "max_regions_per_batch",
        )
        missing = [k for k in required if raw.get(k) in (None, "")]
        if missing:
            raise ValueError(
                f"model_configs.timesfm missing required keys: {missing}. "
                f"See pipelines/dengue/lib/models/timesfm.py docstring."
            )

        revision = str(raw["revision"]).strip().lower()
        if not _REVISION_PATTERN.fullmatch(revision):
            raise ValueError(
                f"model_configs.timesfm.revision must be a full 40-hex commit SHA; "
                f"got {raw['revision']!r}. Pinning a tag/branch is unsafe — the "
                f"checkpoint can move under you."
            )

        positives = {
            "max_context": int(raw["max_context"]),
            "per_core_batch_size": int(raw["per_core_batch_size"]),
            "min_context_weeks": int(raw["min_context_weeks"]),
            "timeout_s": float(raw["timeout_s"]),
            "max_regions_per_batch": int(raw["max_regions_per_batch"]),
        }
        for k, v in positives.items():
            if v <= 0:
                raise ValueError(f"model_configs.timesfm.{k} must be > 0; got {v}")

        return cls(
            huggingface_repo_id=str(raw["huggingface_repo_id"]).strip(),
            revision=revision,
            cache_dir=str(raw["cache_dir"]).strip(),
            max_context=positives["max_context"],
            per_core_batch_size=positives["per_core_batch_size"],
            min_context_weeks=positives["min_context_weeks"],
            timeout_s=positives["timeout_s"],
            max_regions_per_batch=positives["max_regions_per_batch"],
        )


# ---------------------------------------------------------------------------
# Pure helpers — series construction, horizon math, coverage policy
# ---------------------------------------------------------------------------


def build_weekly_series(
    case_df: pd.DataFrame, *, spatial_col: str, cutoff_case: pd.Timestamp
) -> dict[str, pd.Series]:
    """Build a strict weekly grid per region anchored at ``cutoff_case``.

    Every series ends on the same week (the shared anchor that lets a
    single horizon serve the whole batch). Missing weeks inside a
    region's span are zero-filled — a missing weekly report is treated
    as zero cases. Reject duplicates and off-cadence dates.
    """
    if case_df.empty:
        return {}

    df = case_df[["recordDate", spatial_col, "case"]].copy()
    df["recordDate"] = pd.to_datetime(df["recordDate"])
    df = df[df["recordDate"] <= pd.to_datetime(cutoff_case)]

    anchor = pd.to_datetime(cutoff_case).normalize()
    # Anchor every week to the same weekday as cutoff_case.
    anchor_wd = anchor.weekday()
    df["weekStart"] = df["recordDate"] - pd.to_timedelta(
        (df["recordDate"].dt.weekday - anchor_wd) % 7, unit="D"
    )

    series_by_region: dict[str, pd.Series] = {}
    for region, sub in df.groupby(spatial_col, sort=False):
        # Reject off-weekly-cadence input *before* aggregation: every
        # recordDate within a region must sit on the anchor's weekly grid.
        unique_dates = sub["recordDate"].drop_duplicates().sort_values()
        if len(unique_dates) >= 2:
            base = unique_dates.iloc[0]
            day_offsets = (unique_dates - base).dt.days
            off_grid = day_offsets[day_offsets % 7 != 0]
            if not off_grid.empty:
                bad = unique_dates.iloc[off_grid.index[: min(3, len(off_grid))]]
                raise ValueError(
                    f"timesfm: region {region!r} has off-weekly-cadence dates: "
                    f"{[d.date() for d in bad]}"
                )
        weekly = sub.groupby("weekStart")["case"].sum().astype(float).sort_index()
        if weekly.empty:
            continue
        # Zero-fill missing weeks across the region's own span up to cutoff.
        full_index = pd.date_range(start=weekly.index.min(), end=anchor, freq="7D")
        weekly = weekly.reindex(full_index, fill_value=0.0).clip(lower=0.0)
        # Last week's stamp should equal the anchor; rebase the index to that.
        weekly.index = full_index
        series_by_region[str(region)] = weekly

    return series_by_region


def horizon_and_indices_for_targets(
    *, cutoff_case: pd.Timestamp, prediction_dates: list[str]
) -> tuple[int, list[int], list[pd.Timestamp]]:
    """Return ``(horizon, indices, target_timestamps)`` for ``prediction_dates``.

    Index math (floor weekly): ``(d - cutoff_case).days // 7 - 1``. Empty
    ``prediction_dates`` returns ``(0, [], [])`` — the collapsed-horizon
    branch other models also hit. Off-grid or non-positive offsets are a
    hard error.
    """
    if not prediction_dates:
        return 0, [], []

    anchor = pd.to_datetime(cutoff_case).normalize()
    parsed = [pd.to_datetime(d).normalize() for d in prediction_dates]
    indices: list[int] = []
    for d in parsed:
        delta_days = (d - anchor).days
        if delta_days <= 0:
            raise ValueError(
                f"timesfm: prediction date {d.date()} is not strictly after "
                f"cutoff_case {anchor.date()}"
            )
        if delta_days % 7 != 0:
            raise ValueError(
                f"timesfm: prediction date {d.date()} is off the weekly grid "
                f"anchored at cutoff_case {anchor.date()}"
            )
        indices.append(delta_days // 7 - 1)
    horizon = max(indices) + 1
    return horizon, indices, parsed


def _classify_series(
    series_by_region: dict[str, pd.Series], *, min_context_weeks: int
) -> tuple[
    dict[str, pd.Series], dict[str, pd.Series], list[str], list[tuple[str, float]]
]:
    """Split regions into ``(forecastable, all_zero, skipped_thin, gap_log)``.

    ``gap_log`` is a list of ``(region, gap_fraction)`` for diagnostics —
    fraction of weeks in the region's span that were zero-filled.
    """
    forecastable: dict[str, pd.Series] = {}
    all_zero: dict[str, pd.Series] = {}
    skipped: list[str] = []
    gaps: list[tuple[str, float]] = []
    for region, series in series_by_region.items():
        if len(series) < min_context_weeks:
            skipped.append(region)
            continue
        if float(series.sum()) == 0.0:
            all_zero[region] = series
            continue
        # Gap fraction = zero-weeks / total-weeks (a rough non-reporting proxy).
        gap_frac = float((series == 0.0).mean())
        gaps.append((region, gap_frac))
        forecastable[region] = series
    return forecastable, all_zero, skipped, gaps


# ---------------------------------------------------------------------------
# BaseModel implementation
# ---------------------------------------------------------------------------


def _pad_series(series_list: list[np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    lengths = np.array([s.shape[0] for s in series_list], dtype=np.int64)
    max_len = int(lengths.max()) if lengths.size else 0
    out = np.zeros((len(series_list), max_len), dtype=np.float32)
    for i, s in enumerate(series_list):
        out[i, : s.shape[0]] = s
    return out, lengths


def _build_predictions_df(
    *,
    region_order: list[str],
    output: np.ndarray,
    indices: list[int],
    target_timestamps: list[pd.Timestamp],
    spatial_col: str,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for r_idx, region in enumerate(region_order):
        forecast_vec = output[r_idx]
        for k, idx in enumerate(indices):
            value = float(np.clip(forecast_vec[idx], 0.0, None))
            rows.append(
                {
                    spatial_col: region,
                    "recordDate": target_timestamps[k],
                    "prediction": value,
                    "model": "timesFoundationModel",
                }
            )
    return pd.DataFrame(rows)


def forecast(
    *,
    case_df: pd.DataFrame,
    spatial_col: str,
    cutoff_case: pd.Timestamp,
    prediction_dates: list[str],
    params: TimesFmParams,
    workdir: Path | None = None,
    _runner: Any = None,
) -> pd.DataFrame:
    """End-to-end TimesFM forecast for a batch of regions.

    ``_runner`` is a test seam — a callable with the same signature as
    :func:`pipelines.dengue.lib.isolation.run_isolated`. In tests we pass
    a fake; in production we leave it ``None`` and the real subprocess
    runs.
    """
    empty = pd.DataFrame(columns=[spatial_col, "recordDate", "prediction", "model"])

    horizon, indices, target_timestamps = horizon_and_indices_for_targets(
        cutoff_case=cutoff_case, prediction_dates=prediction_dates
    )
    if horizon == 0:
        return empty

    series_by_region = build_weekly_series(
        case_df, spatial_col=spatial_col, cutoff_case=cutoff_case
    )
    if not series_by_region:
        return empty

    forecastable, all_zero, skipped, gaps = _classify_series(
        series_by_region, min_context_weeks=params.min_context_weeks
    )

    for region, gap_frac in gaps:
        if gap_frac > 0.1:
            log.info(
                "timesfm: region %s gap_fraction=%.2f (zero-filled missing weeks)",
                region,
                gap_frac,
            )
    if skipped:
        log.warning(
            "timesfm: %d region(s) below min_context_weeks=%d, skipped: %s",
            len(skipped),
            params.min_context_weeks,
            skipped,
        )

    out_frames: list[pd.DataFrame] = []
    if all_zero:
        for region in all_zero:
            for k, _ in enumerate(indices):
                out_frames.append(
                    pd.DataFrame(
                        [
                            {
                                spatial_col: region,
                                "recordDate": target_timestamps[k],
                                "prediction": 0.0,
                                "model": "timesFoundationModel",
                            }
                        ]
                    )
                )

    if not forecastable:
        if not all_zero:
            raise RuntimeError(
                f"timesfm: every region was skipped — fewer than "
                f"min_context_weeks={params.min_context_weeks} of history. "
                f"Either lower min_context_weeks or pick a coarser spatial_res."
            )
        return pd.concat(out_frames, ignore_index=True) if out_frames else empty

    region_order = list(forecastable.keys())
    series_arrays = [
        np.asarray(forecastable[r].values, dtype=np.float32)[-params.max_context :]
        for r in region_order
    ]
    series_padded, lengths = _pad_series(series_arrays)

    spec = {
        "horizon": horizon,
        "repo_id": params.huggingface_repo_id,
        "revision": params.revision,
        "cache_dir": params.cache_dir,
        "max_context": params.max_context,
        "per_core_batch_size": params.per_core_batch_size,
        "max_regions_per_batch": params.max_regions_per_batch,
    }

    runner = _runner if _runner is not None else run_isolated
    tmp_ctx = None
    if workdir is None:
        tmp_ctx = tempfile.TemporaryDirectory(prefix="timesfm-")
        workdir = Path(tmp_ctx.name)
    try:
        result = runner(
            worker_module=_WORKER_MODULE,
            series=series_padded,
            lengths=lengths,
            spec=spec,
            workdir=workdir,
            timeout_s=params.timeout_s,
            expected_shape=(len(region_order), horizon),
        )
    except IsolatedRunError:
        raise
    finally:
        if tmp_ctx is not None:
            tmp_ctx.cleanup()

    fc_df = _build_predictions_df(
        region_order=region_order,
        output=result.output,
        indices=indices,
        target_timestamps=target_timestamps,
        spatial_col=spatial_col,
    )
    out_frames.append(fc_df)
    return pd.concat(out_frames, ignore_index=True)


# ---------------------------------------------------------------------------
# Registry binding
# ---------------------------------------------------------------------------


from pipelines.dengue.lib.models import ModelContext, register  # noqa: E402


@register("timesfm")
class TimesFmModel:
    def predict(self, ctx: ModelContext) -> pd.DataFrame:
        params = TimesFmParams.from_raw(dict(ctx.model_params or {}))
        return forecast(
            case_df=ctx.case_df,
            spatial_col=ctx.cfg.spatial_res,
            cutoff_case=pd.Timestamp(ctx.cutoff_case),
            prediction_dates=list(ctx.prediction_dates or []),
            params=params,
        )

    def threshold_to_date(self, ctx: ModelContext) -> pd.Timestamp:
        # TimesFM consumes history through cutoff_case; thresholds derive
        # against the same cutoff (same convention as TSE).
        return pd.Timestamp(ctx.cutoff_case)

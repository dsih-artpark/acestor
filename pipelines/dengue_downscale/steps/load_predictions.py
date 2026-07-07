"""Discover and validate the source run's combined predictions CSV."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pandas as pd

from acestor import BaseStep, FileStorage, NoInputs, PipelineContext
from pipelines.dengue_downscale.configs import DownscaleRunConfig
from pipelines.dengue_downscale.results import LoadPredictionsResult

_MODEL_SUFFIXES = ("_nbr_", "_rf_", "_xgb_", "_tse_")


def _resolve_latest_run(
    base_path: Path, parent_level: str, geojson_base: str | None = None
) -> str:
    """Return the name of the most recently modified run dir whose predictions.csv
    is at ``parent_level`` granularity.

    Filters out prior downscale runs (whose predictions.csv is at the *child*
    level): without this guard, 'latest' will happily pick a previous downscale
    output as the source and try to downscale it again, which either crashes
    loudly ('no children in the mapping') or — worse — produces nonsense.

    Naive ``startswith(parent_level + "_")`` is wrong for compound region_types:
    ``"ulb_ward_..."`` starts with ``"ulb_"`` too, so a ULB-ward downscale would
    be picked up as a ULB run and silently break every downstream chain. Guard
    with a longest-prefix match against the region_types available in the
    geojson tree — a rid is a match only if its longest matching prefix equals
    ``parent_level`` exactly.
    """
    known_types: list[str] = []
    if geojson_base is not None:
        try:
            for sub in Path(geojson_base).iterdir():
                if not sub.is_dir():
                    continue
                # dir names are plural in this convention; strip trailing 's'
                name = sub.name[:-1] if sub.name.endswith("s") else sub.name
                known_types.append(name)
        except Exception:
            pass
    # Longest first so `ulb_ward` wins over `ulb` on `ulb_ward_...`.
    known_types.sort(key=len, reverse=True)

    def _classify(rid: str) -> str | None:
        for t in known_types:
            if rid.startswith(f"{t}_"):
                return t
        # Fallback for older callers where we couldn't scan geojsons: use the
        # prefix up to the first underscore. Cheaper, but exposes the compound-
        # type bug above — hence the geojson-aware path is preferred.
        return rid.split("_", 1)[0] if "_" in rid else None

    candidates: list[tuple[float, str]] = []
    for run_dir in base_path.iterdir():
        if not run_dir.is_dir():
            continue
        pred_path = run_dir / "outputs" / "predictions.csv"
        if not pred_path.exists():
            continue
        try:
            head = pd.read_csv(pred_path, usecols=["regionID"], nrows=1)
        except Exception:
            continue
        if head.empty:
            continue
        rid = str(head["regionID"].iloc[0])
        detected = _classify(rid)
        if detected != parent_level:
            continue
        candidates.append((run_dir.stat().st_mtime, run_dir.name))
    if not candidates:
        raise FileNotFoundError(
            f"source_run_id='latest' but no dengue forecast run at parent_level="
            f"{parent_level!r} found in {base_path}. Either run the dengue "
            f"pipeline at this region_type first, or set source_run_id "
            f"explicitly in the downscale config."
        )
    candidates.sort(reverse=True)
    return candidates[0][1]


class LoadPredictionsStep(BaseStep[NoInputs, LoadPredictionsResult]):
    input_type: ClassVar[type] = NoInputs

    def run(self, context: PipelineContext, inputs: NoInputs) -> LoadPredictionsResult:
        cfg = DownscaleRunConfig.from_raw(context.config.get("run") or {})

        storage = context.artifacts
        if not isinstance(storage, FileStorage):
            raise TypeError(
                "LoadPredictionsStep requires a filesystem artifacts storage."
            )

        source_run_id = cfg.source_run_id
        if cfg.is_latest:
            from pipelines.dengue_downscale.configs import DownscaleConfig

            ds_cfg = DownscaleConfig.from_raw(context.config.get("downscale") or {})
            source_run_id = _resolve_latest_run(
                storage.base_path,
                parent_level=ds_cfg.parent_level,
                geojson_base=ds_cfg.geojson_base_path,
            )
            context.log.info(
                "load_predictions: source_run_id='latest' (parent_level=%r) "
                "resolved to %r",
                ds_cfg.parent_level,
                source_run_id,
            )

        results_dir = storage.base_path / source_run_id / "outputs"
        if not results_dir.exists():
            raise FileNotFoundError(
                f"Source run artifacts not found at {results_dir}. "
                f"Run the dengue pipeline with run_id={source_run_id!r} first."
            )

        pred_path = results_dir / "predictions.csv"
        if not pred_path.exists():
            raise FileNotFoundError(
                f"No predictions.csv found in {results_dir}. "
                f"Run the dengue pipeline with run_id={source_run_id!r} first."
            )

        try:
            df = pd.read_csv(pred_path, usecols=["dateOfComputingPrediction"])
        except ValueError as exc:
            raise ValueError(
                f"Could not read 'dateOfComputingPrediction' from {pred_path} "
                f"(source_run_id={cfg.source_run_id!r}). Is this a valid dengue predictions CSV?"
            ) from exc
        run_date = str(df["dateOfComputingPrediction"].iloc[0])

        context.log.info(
            "load_predictions: found %s (run_date=%s, source_run_id=%s)",
            pred_path.name,
            run_date,
            source_run_id,
        )
        return LoadPredictionsResult(
            predictions_csv_path=str(pred_path),
            source_run_id=source_run_id,
            run_date=run_date,
        )

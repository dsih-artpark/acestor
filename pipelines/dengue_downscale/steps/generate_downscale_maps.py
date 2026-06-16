"""Generate per-week static PNG choropleths from child-level predictions.

Closes #64. The downscale pipeline previously had no static map output —
only the in-brief D3 interactive (network-dependent, not viable offline
or for PDF). This step mirrors the main dengue pipeline's
``generate_maps`` step but renders against the child geojson layer the
downscale step already loaded, and writes one PNG per
``(week, model, threshold)`` tuple under ``outputs/maps/``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import pandas as pd

from acestor import BaseStep, PipelineContext
from pipelines.dengue.lib import maps
from pipelines.dengue_downscale.configs import (
    DownscaleConfig,
    DownscaleMapsConfig,
)
from pipelines.dengue_downscale.results import (
    DownscaleMapsResult,
    DownscaleResult,
)


@dataclass(frozen=True)
class GenerateDownscaleMapsInputs:
    downscale_predictions: DownscaleResult


class GenerateDownscaleMapsStep(
    BaseStep[GenerateDownscaleMapsInputs, DownscaleMapsResult]
):
    """Iterate child predictions by (week, model, threshold) and render PNGs."""

    input_type: ClassVar[type] = GenerateDownscaleMapsInputs

    def run(
        self, context: PipelineContext, inputs: GenerateDownscaleMapsInputs
    ) -> DownscaleMapsResult:
        cfg = DownscaleMapsConfig.from_raw(context.config.get("downscale_maps") or {})
        if not cfg.enabled:
            context.log.info("generate_downscale_maps: disabled by config")
            return DownscaleMapsResult(map_paths=())

        ds_cfg = DownscaleConfig.from_raw(context.config.get("downscale") or {})

        # Predictions live in artifact storage; the geojson base lives on the
        # local filesystem (same as the dengue pipeline's maps step).
        pred_csv = context.artifacts.read_text(
            inputs.downscale_predictions.output_csv_path
        )
        import io

        df = pd.read_csv(io.StringIO(pred_csv))

        if df.empty:
            context.log.warning(
                "generate_downscale_maps: child predictions CSV is empty — "
                "nothing to render"
            )
            return DownscaleMapsResult(map_paths=())

        plots_dir = str(context.artifact_fs_path(cfg.output_dir))
        run_date = str(
            (context.config.get("run") or {}).get("run_date", "") or ""
        ).strip()

        # The downscale step writes a child-level geojson directory adjacent to
        # the parent's; gen_plot expects the *parent* of the per-region folder
        # (it tacks on f"{label}s" / f"{label}" itself). Our directory layout is
        # already <base>/<child_level_plural>/, so we hand it the base.
        geojson_base = ds_cfg.geojson_base_path
        if not Path(geojson_base).is_dir():
            raise FileNotFoundError(
                f"generate_downscale_maps: geojson base not found at "
                f"{geojson_base!r}. Check downscale.geojson_base_path."
            )

        generated: list[str] = []
        # All child predictions share one region_type (the child level), so we
        # don't need the dengue maps step's "iterate known regions" sweep.
        child_level = ds_cfg.child_level
        for thisdate in df["startDatePredictedWeek"].unique():
            date_df = df[df["startDatePredictedWeek"] == thisdate]
            for model in date_df["model"].unique():
                model_df = date_df[date_df["model"] == model]
                if model_df.empty:
                    continue
                for threshold in model_df["thresholdMethod"].unique():
                    color_df = model_df[model_df["thresholdMethod"] == threshold]
                    path = maps.gen_plot(
                        color_df,
                        region=child_level,
                        model=model,
                        threshold=threshold,
                        thisdate=str(thisdate),
                        geojson_base=geojson_base,
                        output_dir=plots_dir,
                        figure_title=cfg.figure_title,
                        run_date=run_date,
                    )
                    if path:
                        generated.append(path)

        context.log.info(
            "generate_downscale_maps: %d map(s) written to %s",
            len(generated),
            plots_dir,
        )
        return DownscaleMapsResult(map_paths=tuple(generated))

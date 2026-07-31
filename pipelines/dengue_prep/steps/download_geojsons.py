"""Ensure per-region geojsons for the current ``data.region_type`` are on disk.

Runs first in the prep DAG so parse_case_data / parse_weather_data / the main
dengue pipeline downstream all see a populated ``{base_path}/{region_type}s/``
directory.
"""

from __future__ import annotations

from typing import ClassVar

from acestor import BaseStep, NoInputs, PipelineContext

from pipelines.dengue_prep.configs import PrepGeojsonConfig, _section
from pipelines.dengue_prep.lib.geojson_sources import load_source
from pipelines.dengue_prep.results import PrepGeojsonDownloadResult


class PrepDownloadGeojsonsStep(BaseStep[NoInputs, PrepGeojsonDownloadResult]):
    input_type: ClassVar[type] = NoInputs

    def run(
        self, context: PipelineContext, inputs: NoInputs
    ) -> PrepGeojsonDownloadResult:
        cfg = PrepGeojsonConfig.from_raw(_section(context.config, "data.geojson"))
        if not cfg.base_path:
            raise ValueError("download_geojsons: data.geojson.base_path is required.")
        region_type = str(
            (_section(context.config, "data") or {}).get("region_type", "")
        ).strip()
        if not region_type:
            raise ValueError(
                "download_geojsons: data.region_type is required so the step "
                "knows which level of geojsons to fetch."
            )

        source_cfg: dict = {
            "base_path": cfg.base_path,
            "scope_id": cfg.scope_id,
            "refresh": cfg.refresh,
            "base_url": cfg.base_url,
        }
        context.log.info(
            "download_geojsons: source_mode=%s region_type=%s base_path=%r",
            cfg.source_mode,
            region_type,
            cfg.base_path,
        )
        source = load_source(cfg.source_mode, source_cfg)
        result = source.fetch(region_type, cfg.base_path)

        context.log.info(
            "download_geojsons: %s → %d written + %d cached (total %d) at %s",
            result.region_type,
            result.files_written,
            result.files_cached,
            result.total,
            result.output_dir,
        )
        return PrepGeojsonDownloadResult(
            region_type=result.region_type,
            output_dir=result.output_dir,
            files_written=result.files_written,
            files_cached=result.files_cached,
        )

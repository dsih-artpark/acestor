from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from typing import ClassVar

import pandas as pd

from acestor import BaseStep, NoInputs, PipelineContext
from pipelines.gba_dengue.configs import WeatherDownloadConfig, _section
from pipelines.gba_dengue.sources import filesystem as fs_sources
from pipelines.gba_dengue.sources import s3 as s3_sources
from pipelines.gba_dengue.sources import filesystem as geojson_sources
from pipelines.gba_dengue.sources import cds as cds_sources
from pipelines.gba_dengue.results import WeatherDownloadResult


class DownloadWeatherDataStep(BaseStep[NoInputs, WeatherDownloadResult]):
    input_type: ClassVar[type] = NoInputs

    def __init__(self, source: Any | None = None) -> None:
        if source is not None:
            self.source = source
            return
        backend = os.getenv("GBA_WEATHER_SOURCE_BACKEND", "filesystem").strip().lower()
        if backend == "s3":
            try:
                self.source = s3_sources.get_weather_source()
                return
            except Exception:
                self.source = None
                return
        self.source = fs_sources.get_weather_source()

    def run(self, context: PipelineContext, inputs: NoInputs) -> WeatherDownloadResult:
        cfg = WeatherDownloadConfig.from_raw(
            _section(context.config, "data.weather_download")
        )

        if not cfg.enabled:
            context.log.info("download_weather_data: disabled")
            return WeatherDownloadResult(enabled=False)

        if cfg.source_mode == "cds":
            return self._download_from_cds(context, cfg)

        return self._copy_from_storage(context, cfg)

    def _copy_from_storage(
        self, context: PipelineContext, cfg: WeatherDownloadConfig
    ) -> WeatherDownloadResult:
        """Filesystem mode: copy pre-existing CSVs from a configured storage."""
        source = self.source
        if source is None:
            source = context.require_storage(cfg.source_storage)
        paths = cfg.source_paths
        if not paths:
            paths = source.list_objects(cfg.source_prefix)

        downloaded: list[str] = []
        for src in paths:
            filename = src.replace("\\", "/").split("/")[-1]
            dest = context.artifact_path(f"{cfg.dest_relpath}/{filename}")
            context.artifacts.write(source.read(src), dest)
            downloaded.append(dest)

        context.log.info(
            "download_weather_data: copied %d files from storage", len(downloaded)
        )
        return WeatherDownloadResult(enabled=True, downloaded_files=downloaded)

    def _download_from_cds(
        self, context: PipelineContext, cfg: WeatherDownloadConfig
    ) -> WeatherDownloadResult:
        """CDS mode: download from Copernicus CDS API, cache locally, parse NetCDFs to per-region CSVs."""
        from pipelines.gba_dengue.lib.cds import (
            check_existing_months,
            compute_region_bounds_from_geojsons,
            delete_recent_cache,
            download_months,
            get_missing_months,
            parse_cached_netcdfs,
        )

        cds = cds_sources.get_cds_source()
        geojson_base = geojson_sources.get_geojson_base_dir()

        cache = Path(cds.cache_path)
        cache.mkdir(parents=True, exist_ok=True)

        # --- Resolve region bounds ---
        if cfg.region_bounds:
            region_bounds = tuple(float(x) for x in cfg.region_bounds)
        else:
            context.log.info("region_bounds not set — computing from geojson files")
            region_bounds = compute_region_bounds_from_geojsons(
                Path(geojson_base) / f"{cfg.region_type}s",
                resolution_deg=cfg.bounds_resolution_deg,
            )
        context.log.info("Region bounds (N, W, S, E): %s", region_bounds)

        # --- Resolve date range ---
        start = pd.Timestamp(cfg.start_date)
        end = (
            pd.Timestamp(cfg.end_date)
            if cfg.end_date
            else pd.Timestamp(
                context.config.get("run", {}).get(
                    "run_date", pd.Timestamp.now().strftime("%Y-%m-%d")
                )
            )
        )

        # --- Delete stale cache for current + previous month (always refresh) ---
        delete_recent_cache(cache)

        # --- Determine which months are missing ---
        existing = check_existing_months(cache)
        missing = get_missing_months(start, end, existing)
        context.log.info(
            "CDS download: %d existing months cached, %d months to download",
            len(existing),
            len(missing),
        )

        # --- Download from CDS API ---
        if missing:
            download_months(
                dataset=cds.dataset,
                region_bounds=region_bounds,
                variables=cds.variables,
                months=missing,
                cache_path=cache,
                cds_url=cds.cds_url,
                cds_key=cds.cds_key,
            )

        # --- Parse all cached NetCDFs into per-region per-month CSVs ---
        parsed_out = Path(cds.parsed_output_path) / cfg.region_type
        parsed_out.mkdir(parents=True, exist_ok=True)

        csv_paths = parse_cached_netcdfs(
            cache_path=cache,
            output_path=parsed_out,
            geojson_folder=geojson_base,
            region_type=cfg.region_type,
            w_params=cfg.w_params,
            threshold_km=cfg.threshold_km,
        )

        # --- Copy parsed CSVs into artifacts ---
        downloaded: list[str] = []
        for csv_path in csv_paths:
            filename = Path(csv_path).name
            dest = context.artifact_path(f"{cfg.dest_relpath}/{filename}")
            with open(csv_path, "rb") as fh:
                context.artifacts.write(fh.read(), dest)
            downloaded.append(dest)

        context.log.info(
            "download_weather_data (cds): downloaded %d months, parsed %d CSVs",
            len(missing),
            len(downloaded),
        )
        return WeatherDownloadResult(enabled=True, downloaded_files=downloaded)

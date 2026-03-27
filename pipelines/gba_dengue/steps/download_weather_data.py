from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from typing import ClassVar

import pandas as pd

from acestor import BaseStep, NoInputs, PipelineContext
from acestor.core.sources import FileSystemSource, S3Source
from pipelines.gba_dengue.configs import WeatherDownloadConfig, _section
from pipelines.gba_dengue.sources import filesystem as geojson_sources
from pipelines.gba_dengue.sources import cds as cds_sources
from pipelines.gba_dengue.results import WeatherDownloadResult


class DownloadWeatherDataStep(BaseStep[NoInputs, WeatherDownloadResult]):
    input_type: ClassVar[type] = NoInputs

    def __init__(self, source: Any | None = None) -> None:
        if source is not None:
            self.source = source
            return
        self.source = None

    def _build_source(self, cfg: WeatherDownloadConfig) -> Any | None:
        backend = (cfg.source_backend or "filesystem").strip().lower()
        source_path = (cfg.source_path or "").strip()

        if source_path and backend == "s3" and source_path.startswith("s3://"):
            bucket_and_prefix = source_path[len("s3://") :]
            bucket, _, prefix = bucket_and_prefix.partition("/")
            return S3Source(
                bucket=bucket,
                base_prefix=prefix,
                aws_profile=(os.getenv("AWS_PROFILE", "").strip() or None),
                region=(os.getenv("AWS_REGION", "").strip() or None),
                cache_enabled=cfg.cache_enabled,
                cache_dir=cfg.cache_dir,
                strategy=cfg.cache_strategy,
            )

        if source_path and backend == "filesystem":
            return FileSystemSource(base_path=source_path)

        if backend == "s3":
            if cfg.s3_bucket:
                return S3Source(
                    bucket=cfg.s3_bucket,
                    base_prefix=cfg.s3_prefix,
                    aws_profile=(os.getenv("AWS_PROFILE", "").strip() or None),
                    region=(os.getenv("AWS_REGION", "").strip() or None),
                    cache_enabled=cfg.cache_enabled,
                    cache_dir=cfg.cache_dir,
                    strategy=cfg.cache_strategy,
                )
            return None
        if cfg.filesystem_base_path:
            return FileSystemSource(base_path=cfg.filesystem_base_path)
        return None

    def run(self, context: PipelineContext, inputs: NoInputs) -> WeatherDownloadResult:
        cfg = WeatherDownloadConfig.from_raw(
            _section(context.config, "data.weather_download")
        )

        if not cfg.enabled:
            context.log.info("download_weather_data: disabled")
            return WeatherDownloadResult(enabled=False)

        if cfg.source_mode == "cds":
            return self._download_from_cds(context, cfg)

        if cfg.netcdf_cache_path:
            return self._parse_from_local_netcdf_cache(context, cfg)

        return self._copy_from_storage(context, cfg)

    def _copy_from_storage(
        self, context: PipelineContext, cfg: WeatherDownloadConfig
    ) -> WeatherDownloadResult:
        """Filesystem mode: copy pre-existing CSVs from a configured storage."""
        source = self.source or self._build_source(cfg)
        if source is None:
            source = context.require_storage(cfg.source_storage)
        paths = cfg.source_paths
        if not paths:
            paths = source.list_objects(cfg.source_prefix)

        downloaded: list[str] = []
        total = len(paths)
        for i, src in enumerate(paths, 1):
            filename = src.replace("\\", "/").split("/")[-1]
            context.log.info(
                "download_weather_data: copying file [%d/%d] %s", i, total, filename
            )
            dest = context.artifact_path(f"{cfg.dest_relpath}/{filename}")
            context.artifacts.write(source.read(src), dest)
            downloaded.append(dest)

        context.log.info(
            "download_weather_data: copied %d files from storage", len(downloaded)
        )
        return WeatherDownloadResult(enabled=True, downloaded_files=downloaded)

    def _parse_from_local_netcdf_cache(
        self, context: PipelineContext, cfg: WeatherDownloadConfig
    ) -> WeatherDownloadResult:
        """Local filesystem mode: check zips exist in netcdf_cache_path, parse to per-region CSVs."""
        from pipelines.gba_dengue.lib.cds import parse_cached_netcdfs

        geojson_base = geojson_sources.get_geojson_base_dir()
        cache = Path(cfg.netcdf_cache_path)

        if not cache.exists():
            raise FileNotFoundError(
                f"download_weather_data (local): netcdf_cache_path does not exist: {cache}"
            )

        zip_files = list(cache.rglob("*.zip")) + list(cache.rglob("*.nc"))
        if not zip_files:
            raise FileNotFoundError(
                f"download_weather_data (local): no .zip or .nc files found under {cache}"
            )
        context.log.info(
            "download_weather_data (local): found %d cached files in %s",
            len(zip_files),
            cache,
        )

        base_out = (
            cfg.parsed_output_path or cds_sources.get_cds_source().parsed_output_path
        )
        parsed_out = Path(base_out) / cfg.region_type
        parsed_out.mkdir(parents=True, exist_ok=True)

        csv_paths = parse_cached_netcdfs(
            cache_path=cache,
            output_path=parsed_out,
            geojson_folder=geojson_base,
            region_type=cfg.region_type,
            w_params=cfg.w_params,
            threshold_km=cfg.threshold_km,
        )

        downloaded = [f"filesystem://{csv_path}" for csv_path in csv_paths]

        context.log.info(
            "download_weather_data (local): %d CSVs ready from %s",
            len(downloaded),
            cache,
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

        cache = Path(cfg.netcdf_cache_path or cds.cache_path)
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
        variables = cfg.cds_variables if cfg.cds_variables else cds.variables
        if missing:
            download_months(
                dataset=cds.dataset,
                region_bounds=region_bounds,
                variables=variables,
                months=missing,
                cache_path=cache,
                cds_url=cds.cds_url,
                cds_key=cds.cds_key,
            )

        # --- Parse all cached NetCDFs into per-region per-month CSVs ---
        parsed_out = (
            Path(cfg.parsed_output_path or cds.parsed_output_path) / cfg.region_type
        )
        parsed_out.mkdir(parents=True, exist_ok=True)

        csv_paths = parse_cached_netcdfs(
            cache_path=cache,
            output_path=parsed_out,
            geojson_folder=geojson_base,
            region_type=cfg.region_type,
            w_params=cfg.w_params,
            threshold_km=cfg.threshold_km,
        )

        downloaded = [f"filesystem://{csv_path}" for csv_path in csv_paths]

        context.log.info(
            "download_weather_data (cds): downloaded %d months, %d CSVs ready",
            len(missing),
            len(downloaded),
        )
        return WeatherDownloadResult(enabled=True, downloaded_files=downloaded)

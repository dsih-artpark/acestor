from __future__ import annotations

import os
from pathlib import Path
from typing import Any, ClassVar

import pandas as pd

from acestor import BaseStep, NoInputs, PipelineContext
from acestor.core.sources import FileSystemSource, S3Source
from pipelines.dengue_prep.configs import (
    PrepCDSConfig,
    PrepWeatherDownloadConfig,
    _section,
)
from pipelines.dengue_prep.results import PrepWeatherDownloadResult


class PrepDownloadWeatherDataStep(BaseStep[NoInputs, PrepWeatherDownloadResult]):
    input_type: ClassVar[type] = NoInputs

    def __init__(self, source: Any | None = None) -> None:
        self.source = source

    def _build_source(self, cfg: PrepWeatherDownloadConfig) -> Any | None:
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
        if backend == "s3" and cfg.s3_bucket:
            return S3Source(
                bucket=cfg.s3_bucket,
                base_prefix=cfg.s3_prefix,
                aws_profile=(os.getenv("AWS_PROFILE", "").strip() or None),
                region=(os.getenv("AWS_REGION", "").strip() or None),
                cache_enabled=cfg.cache_enabled,
                cache_dir=cfg.cache_dir,
                strategy=cfg.cache_strategy,
            )
        if cfg.filesystem_base_path:
            return FileSystemSource(base_path=cfg.filesystem_base_path)
        return None

    def run(
        self, context: PipelineContext, inputs: NoInputs
    ) -> PrepWeatherDownloadResult:
        # Shared date_range and region_type flow down from top-level data config
        data_raw = context.config.get("data") or {}
        date_range = data_raw.get("date_range") or {}
        top_region_type = str(data_raw.get("region_type", "")).strip()

        dl_raw = dict(_section(context.config, "data.weather_download"))
        if date_range.get("start") and not dl_raw.get("start_date"):
            dl_raw["start_date"] = date_range["start"]
        if date_range.get("end") and not dl_raw.get("end_date"):
            dl_raw["end_date"] = date_range["end"]
        if top_region_type and not dl_raw.get("region_type"):
            dl_raw["region_type"] = top_region_type

        cfg = PrepWeatherDownloadConfig.from_raw(dl_raw)
        if not cfg.enabled:
            context.log.info("prep download_weather_data: disabled")
            return PrepWeatherDownloadResult(enabled=False)

        if cfg.source_mode == "cds":
            return self._download_from_cds(context, cfg)

        if cfg.source_mode == "openmeteo":
            return self._download_from_openmeteo(context, cfg)

        if cfg.netcdf_cache_path:
            return self._parse_from_local_netcdf_cache(context, cfg)

        return self._copy_from_storage(context, cfg)

    def _copy_from_storage(
        self, context: PipelineContext, cfg: PrepWeatherDownloadConfig
    ) -> PrepWeatherDownloadResult:
        backend = (cfg.source_backend or "filesystem").strip().lower()
        source = self.source or self._build_source(cfg)
        if source is None:
            source = context.require_storage(cfg.source_storage)

        paths = cfg.source_paths or source.list_objects(cfg.source_prefix)
        downloaded: list[str] = []

        if backend == "filesystem" and cfg.source_path:
            for src in paths:
                if not str(src).endswith(".csv"):
                    continue
                full_path = str(Path(cfg.source_path) / src)
                downloaded.append(f"filesystem://{full_path}")
        elif backend == "s3":
            for src in paths:
                source.read(src)
                downloaded.append(f"s3://{src}")
        else:
            for src in paths:
                filename = src.replace("\\", "/").split("/")[-1]
                dest = context.artifact_path(f"{cfg.dest_relpath}/{filename}")
                context.artifacts.write(source.read(src), dest)
                downloaded.append(dest)

        context.log.info(
            "prep download_weather_data: referencing %d files (backend=%s)",
            len(downloaded),
            backend,
        )
        return PrepWeatherDownloadResult(enabled=True, downloaded_files=downloaded)

    def _parse_from_local_netcdf_cache(
        self, context: PipelineContext, cfg: PrepWeatherDownloadConfig
    ) -> PrepWeatherDownloadResult:
        from pipelines.dengue.lib.cds import parse_cached_netcdfs

        geojson_base = str(
            ((context.config.get("data") or {}).get("geojson") or {}).get(
                "base_path", ""
            )
        ).strip()
        cache = Path(cfg.netcdf_cache_path)

        if not cache.exists():
            raise FileNotFoundError(
                f"prep download_weather_data: netcdf_cache_path does not exist: {cache}"
            )

        zip_files = list(cache.rglob("*.zip")) + list(cache.rglob("*.nc"))
        if not zip_files:
            raise FileNotFoundError(
                f"prep download_weather_data: no .zip or .nc files found under {cache}"
            )

        cds = PrepCDSConfig.from_env()
        base_out = cfg.parsed_output_path or cds.parsed_output_path
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
        downloaded = [f"filesystem://{p}" for p in csv_paths]
        context.log.info(
            "prep download_weather_data: %d CSVs ready from cache", len(downloaded)
        )
        return PrepWeatherDownloadResult(enabled=True, downloaded_files=downloaded)

    def _download_from_cds(
        self, context: PipelineContext, cfg: PrepWeatherDownloadConfig
    ) -> PrepWeatherDownloadResult:
        from pipelines.dengue.lib.cds import (
            check_existing_months,
            compute_region_bounds_from_geojsons,
            delete_recent_cache,
            download_months,
            get_missing_months,
            parse_cached_netcdfs,
        )

        cds = PrepCDSConfig.from_env()
        geojson_base = str(
            ((context.config.get("data") or {}).get("geojson") or {}).get(
                "base_path", ""
            )
        ).strip()

        cache = Path(cfg.netcdf_cache_path or cds.cache_path)
        cache.mkdir(parents=True, exist_ok=True)

        if cfg.region_bounds:
            region_bounds = tuple(float(x) for x in cfg.region_bounds)
        else:
            context.log.info("region_bounds not set — computing from geojson files")
            region_bounds = compute_region_bounds_from_geojsons(
                Path(geojson_base) / f"{cfg.region_type}s",
                resolution_deg=cfg.bounds_resolution_deg,
            )
        context.log.info("Region bounds (N, W, S, E): %s", region_bounds)

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

        delete_recent_cache(cache)
        existing = check_existing_months(cache)
        missing = get_missing_months(start, end, existing)
        context.log.info(
            "CDS download: %d existing months cached, %d months to download",
            len(existing),
            len(missing),
        )

        variables = cfg.cds_variables or cds.variables
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

        downloaded = [f"filesystem://{p}" for p in csv_paths]
        context.log.info(
            "prep download_weather_data (cds): %d months downloaded, %d CSVs ready",
            len(missing),
            len(downloaded),
        )
        return PrepWeatherDownloadResult(enabled=True, downloaded_files=downloaded)

    def _download_from_openmeteo(
        self, context: PipelineContext, cfg: PrepWeatherDownloadConfig
    ) -> PrepWeatherDownloadResult:
        """OpenMeteo mode: fetch ERA5 reanalysis from Open-Meteo archive API (free, no credentials).

        Writes one CSV per month to datasets/openmeteo/{region_type}/{year}/{year}_{mm}.csv.
        Incremental: skips months whose CSV already exists and has data.
        """
        from pipelines.dengue_prep.lib.openmeteo import (
            delete_recent_months,
            download_range,
            get_missing_chunks,
            get_missing_months,
            load_region_gdfs,
        )

        geojson_base = str(
            ((context.config.get("data") or {}).get("geojson") or {}).get(
                "base_path", ""
            )
        ).strip()
        if not geojson_base:
            raise ValueError(
                "prep download_weather_data (openmeteo): data.geojson.base_path is required"
            )

        region_gdfs = load_region_gdfs(geojson_base, cfg.region_type)
        context.log.info(
            "prep download_weather_data (openmeteo): %d regions loaded for region_type=%s",
            len(region_gdfs),
            cfg.region_type,
        )

        output_path = (
            Path(cfg.parsed_output_path or f"datasets/openmeteo/{cfg.region_type}")
            / cfg.region_type
        )
        output_path.mkdir(parents=True, exist_ok=True)

        start = pd.Timestamp(cfg.start_date)
        run_date_str = ((context.config.get("run") or {}).get("run_date") or "").strip()
        end_str = (
            cfg.end_date or run_date_str or pd.Timestamp.now().strftime("%Y-%m-%d")
        )
        end = pd.Timestamp(end_str)
        # ERA5 archive has a ~5-day lag — cap end to avoid 400 errors on recent dates
        openmeteo_max = pd.Timestamp.now().normalize() - pd.Timedelta(days=5)
        if end > openmeteo_max:
            context.log.info(
                "prep download_weather_data (openmeteo): capping end date %s → %s (ERA5 5-day lag)",
                end.date(),
                openmeteo_max.date(),
            )
            end = openmeteo_max

        # Always re-fetch the current and previous month — their CSVs may be
        # partial (month still in progress or ERA5 lag). Mirrors CDS behaviour.
        delete_recent_months(output_path, n=1)

        chunk_months = int(cfg.chunk_months) if hasattr(cfg, "chunk_months") else 12
        chunks = get_missing_chunks(start, end, output_path, chunk_months=chunk_months)
        missing_count = len(get_missing_months(start, end, output_path))

        context.log.info(
            "prep download_weather_data (openmeteo): "
            "%d months missing · %d regions · chunk_size=%d months "
            "→ %d API calls (1 per chunk, all regions batched)",
            missing_count,
            len(region_gdfs),
            chunk_months,
            len(chunks),
        )

        convert_units = (
            bool(cfg.convert_units) if hasattr(cfg, "convert_units") else False
        )
        downloaded: list[str] = []
        for i, (chunk_start, chunk_end) in enumerate(chunks, 1):
            context.log.info(
                "prep download_weather_data (openmeteo): chunk [%d/%d] %s → %s",
                i,
                len(chunks),
                chunk_start.date(),
                chunk_end.date(),
            )
            try:
                written = download_range(
                    chunk_start,
                    chunk_end,
                    region_gdfs,
                    output_path,
                    convert_units=convert_units,
                )
                downloaded.extend(f"filesystem://{p}" for p in written)
            except Exception:
                context.log.exception(
                    "prep download_weather_data (openmeteo): chunk [%d/%d] failed",
                    i,
                    len(chunks),
                )

        # Also include already-cached months
        for year_dir in sorted(output_path.iterdir()):
            if not year_dir.is_dir():
                continue
            for f in sorted(year_dir.glob("*.csv")):
                ref = f"filesystem://{f}"
                if ref not in downloaded and sum(1 for _ in f.open()) > 1:
                    downloaded.append(ref)

        context.log.info(
            "prep download_weather_data (openmeteo): %d total CSVs ready",
            len(downloaded),
        )
        return PrepWeatherDownloadResult(enabled=True, downloaded_files=downloaded)

from __future__ import annotations

import dataclasses
from calendar import monthrange
from pathlib import Path
from typing import Any, ClassVar

import pandas as pd

from acestor import BaseStep, NoInputs, PipelineContext
from pipelines.dengue_prep.configs import (
    PrepCDSConfig,
    PrepWeatherDownloadConfig,
    _section,
)
from pipelines.dengue_prep.results import PrepWeatherDownloadResult


class PrepDownloadWeatherDataStep(BaseStep[NoInputs, PrepWeatherDownloadResult]):
    input_type: ClassVar[type] = NoInputs

    def run(
        self, context: PipelineContext, inputs: NoInputs
    ) -> PrepWeatherDownloadResult:
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

        # Legacy: parse pre-downloaded NetCDF files without hitting the CDS API
        if cfg.netcdf_cache_path:
            return self._parse_from_local_netcdf_cache(context, cfg)

        return self._download_from_source(context, cfg)

    # ------------------------------------------------------------------
    # Plugin-based download (openmeteo, filesystem, ap_state_api, external)
    # ------------------------------------------------------------------

    def _download_from_source(
        self, context: PipelineContext, cfg: PrepWeatherDownloadConfig
    ) -> PrepWeatherDownloadResult:
        from pipelines.dengue_prep.lib.weather_sources import (
            load_source,
            normalize_records,
        )

        data_raw = context.config.get("data") or {}
        geojson_base = str((data_raw.get("geojson") or {}).get("base_path", "")).strip()

        # Merge the RAW YAML weather_download section on top of the dataclass
        # output so plugin-specific fields (scope_id, base_url, refresh, …)
        # survive parsing. PrepWeatherDownloadConfig only declares the fields
        # the core pipeline uses; without this merge the dataclass silently
        # strips plugin fields and the source blows up with 'X is required'.
        # Same pattern as download_case_data.py did in PR #108.
        raw_weather_section = dict(data_raw.get("weather_download") or {})
        source_config: dict[str, Any] = {
            **dataclasses.asdict(cfg),
            **raw_weather_section,
            "geojson_base_path": geojson_base,
        }

        source = load_source(cfg.source_mode, source_config)

        start = pd.Timestamp(cfg.start_date)
        run_date = ((context.config.get("run") or {}).get("run_date") or "").strip()
        end_str = cfg.end_date or run_date or pd.Timestamp.now().strftime("%Y-%m-%d")
        end = pd.Timestamp(end_str)

        if not source.should_persist:
            return self._reference_source_files(context, cfg)

        return self._fetch_and_persist(
            context, cfg, source, source_config, normalize_records, start, end
        )

    def _reference_source_files(
        self, context: PipelineContext, cfg: PrepWeatherDownloadConfig
    ) -> PrepWeatherDownloadResult:
        """For should_persist=False sources: list source CSVs directly, no fetch/write."""
        source_path = cfg.source_path or cfg.filesystem_base_path or ""
        if not source_path:
            context.log.warning(
                "download_weather_data (%s): no source_path configured", cfg.source_mode
            )
            return PrepWeatherDownloadResult(enabled=True, downloaded_files=[])

        csv_files = sorted(
            f for f in Path(source_path).rglob("*.csv") if not f.name.startswith(".")
        )
        downloaded = [f"filesystem://{f.resolve()}" for f in csv_files]
        context.log.info(
            "download_weather_data (%s): referencing %d files from %s",
            cfg.source_mode,
            len(downloaded),
            source_path,
        )
        return PrepWeatherDownloadResult(enabled=True, downloaded_files=downloaded)

    def _fetch_and_persist(
        self,
        context: PipelineContext,
        cfg: PrepWeatherDownloadConfig,
        source: Any,
        source_config: dict[str, Any],
        normalize_records: Any,
        start: pd.Timestamp,
        end: pd.Timestamp,
    ) -> PrepWeatherDownloadResult:
        """Incremental fetch: append-only, no deletions.

        For each month in range:
          - Past months with a complete CSV → skip.
          - Current (in-progress) month or missing month → find max date already
            stored, fetch only from max_date+1, append to existing file.
        """
        output_path = (
            Path(cfg.parsed_output_path or f"datasets/{cfg.source_mode}")
            / cfg.region_type
        )
        output_path.mkdir(parents=True, exist_ok=True)

        from pipelines.dengue_prep.lib.summary import (
            MonthFetch,
            WeatherDownloadStats,
            render_weather_download_summary,
            write_markdown_report,
        )

        dl_stats = WeatherDownloadStats(
            source_mode=cfg.source_mode, region_type=cfg.region_type
        )

        now = pd.Timestamp.now().normalize()
        current_ym = (now.year, now.month)
        # Backfill tail: force-refetch the last N days ending today, even if
        # the file already covers them. Catches late corrections upstream
        # (ERA5 revisions land ~5 days after the initial fetch; the dashboard
        # weather sync re-imports whatever ERA5 publishes).
        backfill_cutoff = now - pd.Timedelta(days=max(0, cfg.backfill_days))
        downloaded: list[str] = []

        for year, month in _months_in_range(start, end):
            _, last_day = monthrange(year, month)
            month_start = f"{year}-{month:02d}-01"
            month_end = min(
                pd.Timestamp(f"{year}-{month:02d}-{last_day:02d}"), end
            ).strftime("%Y-%m-%d")
            dest = output_path / str(year) / f"{year}_{month:02d}.csv"
            is_current = (year, month) == current_ym
            # A past month is only "backfill-eligible" if its month_end falls
            # inside the backfill window.
            in_backfill_window = pd.Timestamp(month_end) >= backfill_cutoff

            # Past months — skip only if the file actually covers through
            # month_end AND is outside the backfill window. A previous run may
            # have hit Open-Meteo's archive lag mid-month, leaving a gap at
            # the tail; when the month rolls into "past" the code used to
            # treat any existing file as complete, freezing the gap forever.
            # Now we look at the max date on disk and fall through to the
            # resume-fetch branch if it's short OR if it's inside the backfill
            # window (in which case we overwrite the tail).
            if (
                not is_current
                and not in_backfill_window
                and dest.exists()
                and _file_has_data(dest)
            ):
                max_date = _max_date_in_file(dest)
                if max_date and pd.Timestamp(max_date) >= pd.Timestamp(month_end):
                    downloaded.append(f"filesystem://{dest.resolve()}")
                    dl_stats.months_skipped_complete += 1
                    continue

            # Determine fetch start:
            # - If we're in the backfill window, refetch from backfill_cutoff
            #   (or month_start, whichever is later) to overwrite stale tail rows.
            # - Otherwise resume from the last date already stored.
            if in_backfill_window and dest.exists() and _file_has_data(dest):
                fetch_start = max(pd.Timestamp(month_start), backfill_cutoff).strftime(
                    "%Y-%m-%d"
                )
            elif dest.exists() and _file_has_data(dest):
                max_date = _max_date_in_file(dest)
                fetch_start = (
                    (pd.Timestamp(max_date) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
                    if max_date
                    else month_start
                )
            else:
                fetch_start = month_start

            if fetch_start > month_end:
                if dest.exists():
                    downloaded.append(f"filesystem://{dest.resolve()}")
                continue

            context.log.info(
                "download_weather_data (%s): %d-%02d fetching %s → %s",
                cfg.source_mode,
                year,
                month,
                fetch_start,
                month_end,
            )

            try:
                records = source.get_weather_all_regions(
                    fetch_start, month_end, cfg.region_type, source_config
                )
                records = normalize_records(
                    records, cfg.temperature_unit, cfg.precipitation_unit
                )
            except Exception:
                context.log.exception(
                    "download_weather_data (%s): fetch failed %s → %s — skipping",
                    cfg.source_mode,
                    fetch_start,
                    month_end,
                )
                continue

            if not records:
                context.log.info(
                    "download_weather_data (%s): no records returned %s → %s",
                    cfg.source_mode,
                    fetch_start,
                    month_end,
                )
                dl_stats.months_upstream_empty += 1
                if dest.exists():
                    downloaded.append(f"filesystem://{dest.resolve()}")
                continue

            dest.parent.mkdir(parents=True, exist_ok=True)
            df_new = pd.DataFrame(records)

            if dest.exists() and _file_has_data(dest):
                df_existing = pd.read_csv(dest)
                df_combined = pd.concat([df_existing, df_new], ignore_index=True)
                df_combined.drop_duplicates(
                    subset=["region_id", "date"], keep="last", inplace=True
                )
                df_combined.to_csv(dest, index=False)
            else:
                df_new.to_csv(dest, index=False)

            downloaded.append(f"filesystem://{dest.resolve()}")
            dl_stats.months_fetched.append(
                MonthFetch(
                    year_month=f"{year}-{month:02d}",
                    fetched_start=fetch_start,
                    fetched_end=month_end,
                    rows_added=len(df_new),
                )
            )

        context.log.info(
            "download_weather_data (%s): %d monthly files ready",
            cfg.source_mode,
            len(downloaded),
        )

        # ── Rich terminal + append to markdown report ─────────────────────
        render_weather_download_summary(dl_stats)
        report_path = Path(context.artifact_fs_path("outputs/prep_summary.md"))
        write_markdown_report(report_path, weather_download=dl_stats)

        return PrepWeatherDownloadResult(enabled=True, downloaded_files=downloaded)

    # ------------------------------------------------------------------
    # CDS (legacy — unchanged)
    # ------------------------------------------------------------------

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
            "prep download_weather_data: %d CSVs ready from netcdf cache",
            len(downloaded),
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


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _months_in_range(start: pd.Timestamp, end: pd.Timestamp) -> list[tuple[int, int]]:
    months: list[tuple[int, int]] = []
    current = pd.Timestamp(start.year, start.month, 1)
    end_month = pd.Timestamp(end.year, end.month, 1)
    while current <= end_month:
        months.append((current.year, current.month))
        current += pd.DateOffset(months=1)
    return months


def _file_has_data(path: Path) -> bool:
    try:
        return sum(1 for _ in path.open()) > 1
    except Exception:
        return False


def _max_date_in_file(path: Path) -> str | None:
    try:
        df = pd.read_csv(path, usecols=["date"])
        if df.empty:
            return None
        return str(df["date"].max())
    except Exception:
        return None

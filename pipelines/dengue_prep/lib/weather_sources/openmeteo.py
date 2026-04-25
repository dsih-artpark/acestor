"""OpenMeteo weather source.

Wraps the existing lib/openmeteo.py fetch helpers. Returns records in native
OpenMeteo units (°C for temperature/dew point, mm for precipitation).
The step normalizes to Kelvin/metres via temperature_unit / precipitation_unit config.

Batching (50 regions per HTTP call) is handled internally — the step just
asks for a date range and gets back all records.

Overrides get_weather_for_regions to use lat/lon directly when locations
carry coordinate pairs, avoiding a full-state fetch and filter.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

import pandas as pd

from pipelines.dengue_prep.lib.weather_sources import WeatherSource

log = logging.getLogger(__name__)


class Source(WeatherSource):
    should_persist = True

    def get_weather_all_regions(
        self,
        start_date: str,
        end_date: str,
        region_type: str,
        config: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        from pipelines.dengue_prep.lib import openmeteo as _om

        geojson_base = str(config.get("geojson_base_path", "")).strip()
        if not geojson_base:
            raise ValueError("openmeteo source requires geojson_base_path in config")

        region_gdfs = _om.load_region_gdfs(geojson_base, region_type)
        log.info(
            "openmeteo: %d regions loaded for region_type=%s",
            len(region_gdfs),
            region_type,
        )

        # ERA5 archive has ~5-day lag — cap silently
        start_ts = pd.Timestamp(start_date)
        end_ts = pd.Timestamp(end_date)
        openmeteo_max = pd.Timestamp.now().normalize() - pd.Timedelta(days=5)
        if end_ts > openmeteo_max:
            log.info(
                "openmeteo: capping end %s → %s (ERA5 5-day lag)",
                end_ts.date(),
                openmeteo_max.date(),
            )
            end_ts = openmeteo_max

        if end_ts < start_ts:
            log.info(
                "openmeteo: skipping fetch — capped end %s is before start %s "
                "(chunk is entirely within the ERA5 5-day lag window)",
                end_ts.date(),
                start_ts.date(),
            )
            return []

        return self._fetch_regions(
            region_gdfs,
            start_date,
            end_ts.strftime("%Y-%m-%d"),
            int(config.get("region_batch_size", 50)),
        )

    def get_weather_for_regions(
        self,
        locations: list[dict[str, Any]],
        start_date: str,
        end_date: str,
        region_type: str,
        config: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        if any("lat" in loc and "lon" in loc for loc in locations):
            return self._fetch_by_coordinates(
                locations,
                start_date,
                end_date,
                int(config.get("region_batch_size", 50)),
            )
        return super().get_weather_for_regions(
            locations, start_date, end_date, region_type, config
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_regions(
        self,
        region_gdfs: list,
        start_date: str,
        end_date: str,
        batch_size: int,
    ) -> list[dict[str, Any]]:
        from pipelines.dengue_prep.lib import openmeteo as _om

        lats = [_om._centroid(gdf)[0] for gdf in region_gdfs]
        lons = [_om._centroid(gdf)[1] for gdf in region_gdfs]
        n_batches = (len(region_gdfs) + batch_size - 1) // batch_size
        records: list[dict[str, Any]] = []

        for i in range(n_batches):
            sl = slice(i * batch_size, (i + 1) * batch_size)
            try:
                dfs = _om._fetch_multi(lats[sl], lons[sl], start_date, end_date)
            except Exception:
                log.exception(
                    "openmeteo: batch %d/%d failed (%s→%s) — skipping",
                    i + 1,
                    n_batches,
                    start_date,
                    end_date,
                )
                continue
            for gdf, df in zip(region_gdfs[sl], dfs):
                row = gdf.iloc[0]
                records.extend(_df_to_records(df, row))

        return records

    def _fetch_by_coordinates(
        self,
        locations: list[dict[str, Any]],
        start_date: str,
        end_date: str,
        batch_size: int,
    ) -> list[dict[str, Any]]:
        from pipelines.dengue_prep.lib import openmeteo as _om

        lats = [loc["lat"] for loc in locations]
        lons = [loc["lon"] for loc in locations]
        n_batches = (len(locations) + batch_size - 1) // batch_size
        records: list[dict[str, Any]] = []

        for i in range(n_batches):
            sl = slice(i * batch_size, (i + 1) * batch_size)
            try:
                dfs = _om._fetch_multi(lats[sl], lons[sl], start_date, end_date)
            except Exception:
                log.exception(
                    "openmeteo: coordinate batch %d/%d failed — skipping",
                    i + 1,
                    n_batches,
                )
                continue
            for loc, df in zip(locations[sl], dfs):
                records.extend(_df_to_records(df, loc))

        return records


def _df_to_records(df: "pd.DataFrame", meta: Any) -> list[dict[str, Any]]:
    """Convert a per-region OpenMeteo DataFrame to the standard record format."""
    if hasattr(meta, "get"):
        region_id = str(meta.get("region_id", ""))
        name = str(meta.get("name", ""))
        parent = str(meta.get("parent", ""))
        parent_name = str(meta.get("parent_name", ""))
    else:
        # GeoDataFrame row
        region_id = str(meta.get("region_id", "")) if hasattr(meta, "get") else ""
        name = str(getattr(meta, "name", ""))
        parent = str(getattr(meta, "parent", ""))
        parent_name = str(getattr(meta, "parent_name", ""))

    records = []
    for _, row in df.iterrows():
        records.append(
            {
                "date": str(row["time"]),
                "region_id": region_id,
                "t2m": float(row["t2m"]),
                "d2m": float(row["d2m"]),
                "tp": float(row["tp"]),
                "name": name,
                "parent": parent,
                "parent_name": parent_name,
            }
        )
    return records

"""Filesystem geojson source — files already on disk, verify + report.

This is the historical default. Geojsons are shipped with the repo or dropped
into ``{base_path}/{region_type}s/`` manually; this source does nothing beyond
counting what's already there.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from pipelines.dengue_prep.lib.geojson_sources import (
    GeojsonFetchResult,
    GeojsonSource,
    log,
)


class Source(GeojsonSource):
    @classmethod
    def build(
        cls, config: Mapping[str, Any]
    ) -> "Source":  # noqa: ARG003 — no config needed
        return cls()

    def fetch(self, region_type: str, base_path: str) -> GeojsonFetchResult:
        target = Path(base_path) / f"{region_type}s"
        if not target.exists():
            raise FileNotFoundError(
                f"geojson_sources.filesystem: expected {target} to exist "
                f"(source_mode=filesystem — files must already be on disk). "
                f"Set data.geojson.source_mode=dashboard to auto-fetch, or "
                f"drop the geojson files into that directory manually."
            )
        files = sorted(target.glob(f"{region_type}_*.geojson"))
        log.info(
            "geojson_sources.filesystem: %d existing %s geojson(s) in %s",
            len(files),
            region_type,
            target,
        )
        return GeojsonFetchResult(
            region_type=region_type,
            output_dir=str(target),
            files_written=0,
            files_cached=len(files),
        )

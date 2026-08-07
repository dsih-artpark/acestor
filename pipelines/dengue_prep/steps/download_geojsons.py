"""Ensure per-region geojsons for the current ``data.region_type`` are on disk.

Runs first in the prep DAG so parse_case_data / parse_weather_data / the main
dengue pipeline downstream all see a populated ``{base_path}/{region_type}s/``
directory.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

from acestor import BaseStep, NoInputs, PipelineContext

from pipelines.dengue_prep.configs import PrepGeojsonConfig, _section
from pipelines.dengue_prep.lib.geojson_sources import load_source
from pipelines.dengue_prep.results import PrepGeojsonDownloadResult


def _validate_geojsons(output_dir: str, region_type: str) -> None:
    """Assert every geojson in the just-fetched dir carries a child key
    (``region_id`` or ``id``).

    Historically prep silently skipped features missing ``region_id``, so a
    bad export (e.g. dashboard forgetting to populate the field on a
    specific level) manifested two hops later as a mysterious
    ``No parent→child mapping found`` in downscale — a silent-upstream /
    loud-downstream anti-pattern. Fail here instead, right next to the data
    that's wrong.

    Parent info is intentionally NOT checked here — root levels (state,
    gulb) legitimately have no parent, and non-root exports with missing
    parents already surface loudly in downscale/rollup with the same
    "no mapping" error. Only ``region_id`` was the silent-vs-loud trap.
    """
    dir_path = Path(output_dir)
    if not dir_path.exists():
        return  # nothing to validate — the download step itself will have failed
    missing: list[str] = []
    for path in sorted(dir_path.glob("*.geojson")):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue  # unreadable file surfaces elsewhere; not our concern
        features = data.get("features") or ([data] if isinstance(data, dict) else [])
        for feat in features:
            props = feat.get("properties", {}) if isinstance(feat, dict) else {}
            if not (props.get("region_id") or props.get("id")):
                missing.append(path.name)
                break
    if missing:
        raise ValueError(
            f"download_geojsons: geojson data-contract violation in "
            f"{dir_path} ({region_type}): missing `region_id`/`id` in "
            f"{len(missing)} file(s), first: {missing[:3]}. "
            "Fix the source (dashboard export) before this pipeline can "
            "proceed — silent skipping here has historically caused "
            "downstream downscale/rollup to fail two hops away with an "
            "unhelpful error."
        )


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
        _validate_geojsons(result.output_dir, result.region_type)
        return PrepGeojsonDownloadResult(
            region_type=result.region_type,
            output_dir=result.output_dir,
            files_written=result.files_written,
            files_cached=result.files_cached,
        )

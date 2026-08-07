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
    """Assert every geojson in the just-fetched dir carries the fields our
    downstream pipelines rely on: ``region_id`` (child key) and — for
    non-root levels — ``parent_id`` (parent chain walk).

    Historically this was skipped, so a bad export (e.g. dashboard forgetting
    to populate ``region_id`` on a specific level) manifested two hops later
    as a mysterious ``No parent→child mapping found`` in downscale — a
    silent-upstream / loud-downstream anti-pattern. Fail here instead, right
    next to the data that's wrong.

    Both keys are also read with an ``id`` fallback in the downstream helpers
    for defense-in-depth, but if either is missing outright on the source
    geojson, that's a real data-contract violation and we surface it.
    """
    dir_path = Path(output_dir)
    if not dir_path.exists():
        return  # nothing to validate — the download step itself will have failed
    missing_region_id: list[str] = []
    missing_parent_id: list[str] = []
    for path in sorted(dir_path.glob("*.geojson")):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue  # unreadable file surfaces elsewhere; not our concern
        features = data.get("features") or ([data] if isinstance(data, dict) else [])
        for feat in features:
            props = feat.get("properties", {}) if isinstance(feat, dict) else {}
            if not (props.get("region_id") or props.get("id")):
                missing_region_id.append(path.name)
                break
            # 'state' (or any root-level) has no parent — skip parent check for it.
            if region_type != "state" and not (
                props.get("parent_id") or props.get("parent")
            ):
                missing_parent_id.append(path.name)
                break
    problems = []
    if missing_region_id:
        problems.append(
            f"missing `region_id`/`id` in {len(missing_region_id)} "
            f"file(s), first: {missing_region_id[:3]}"
        )
    if missing_parent_id:
        problems.append(
            f"missing `parent_id`/`parent` in {len(missing_parent_id)} "
            f"file(s), first: {missing_parent_id[:3]}"
        )
    if problems:
        raise ValueError(
            f"download_geojsons: geojson data-contract violation in "
            f"{dir_path} ({region_type}): "
            + "; ".join(problems)
            + ". Fix the source (dashboard export) before this pipeline "
            "can proceed — silent skipping here has historically caused "
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

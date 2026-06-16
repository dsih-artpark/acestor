"""Tests for the static-PNG downscale maps step (#64).

Doesn't exercise matplotlib — gen_plot is mocked. The step's job is
iteration + dispatch + per-tuple file naming; that's what these tests
cover. Actual rendering is tested transitively wherever the dengue
pipeline's generate_maps step is covered.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pandas as pd
import pytest

from acestor.io.storage import FileStorage
from pipelines.dengue_downscale.configs import DownscaleMapsConfig
from pipelines.dengue_downscale.results import DownscaleResult
from pipelines.dengue_downscale.steps.generate_downscale_maps import (
    GenerateDownscaleMapsInputs,
    GenerateDownscaleMapsStep,
)


# ---------------------------------------------------------------------------
# DownscaleMapsConfig parsing
# ---------------------------------------------------------------------------


def test_downscale_maps_config_defaults():
    cfg = DownscaleMapsConfig.from_raw({})
    assert cfg.enabled is True
    assert cfg.output_dir == "outputs/maps"
    assert cfg.figure_title == "Dengue risk map"


def test_downscale_maps_config_disabled_flag():
    cfg = DownscaleMapsConfig.from_raw({"enabled": False})
    assert cfg.enabled is False


def test_downscale_maps_config_custom_output_and_title():
    cfg = DownscaleMapsConfig.from_raw(
        {"output_dir": "outputs/static_maps", "figure_title": "AP Mandal Risk"}
    )
    assert cfg.output_dir == "outputs/static_maps"
    assert cfg.figure_title == "AP Mandal Risk"


# ---------------------------------------------------------------------------
# Step iteration / dispatch
# ---------------------------------------------------------------------------


@dataclass
class _FakeContext:
    config: dict[str, Any]
    run_id: str
    artifacts: FileStorage
    completed_steps: list[str] = field(default_factory=list)

    def artifact_path(self, relpath: str) -> str:
        return f"{self.run_id}/{relpath}"

    def artifact_fs_path(self, relpath: str) -> Path:
        return self.artifacts.base_path / self.artifact_path(relpath)

    @property
    def log(self):
        import logging

        return logging.getLogger("test")


def _make_ctx(tmp_path: Path, geojson_base: Path) -> _FakeContext:
    return _FakeContext(
        config={
            "downscale": {
                "parent_level": "district",
                "child_level": "mandal",
                "cases_csv": str(tmp_path / "cases.csv"),
                "geojson_base_path": str(geojson_base),
            },
            "downscale_maps": {},
        },
        run_id="rid-maps",
        artifacts=FileStorage(base_path=tmp_path),
    )


def _make_predictions_csv(storage: FileStorage, run_id: str) -> str:
    """Write a minimal predictions CSV with 2 weeks × 2 models × 1 threshold."""
    rows = []
    for week in ("2026-03-08", "2026-03-15"):
        for model in ("randomForestRegression", "ensembleModel"):
            for region in ("mandal_502_01", "mandal_502_02"):
                rows.append(
                    {
                        "regionID": region,
                        "startDatePredictedWeek": week,
                        "model": model,
                        "thresholdMethod": "historical",
                        "prediction": 10.0,
                        "predictionZone": 2,
                    }
                )
    df = pd.DataFrame(rows)
    key = f"{run_id}/outputs/predictions.csv"
    storage.write_text(df.to_csv(index=False), key)
    return key


def test_step_iterates_each_week_model_threshold_combination(tmp_path: Path):
    """4 PNGs expected: 2 weeks × 2 models × 1 threshold."""
    geojson_base = tmp_path / "geojsons"
    (geojson_base / "mandals").mkdir(parents=True)
    ctx = _make_ctx(tmp_path, geojson_base)
    pred_key = _make_predictions_csv(ctx.artifacts, ctx.run_id)
    ds_result = DownscaleResult(
        output_csv_path=pred_key, n_parent_rows=2, n_child_rows=4
    )
    with patch(
        "pipelines.dengue_downscale.steps.generate_downscale_maps.maps.gen_plot",
        return_value="/tmp/fake.png",
    ) as gen:
        out = GenerateDownscaleMapsStep().run(
            ctx, GenerateDownscaleMapsInputs(downscale_predictions=ds_result)
        )
    assert gen.call_count == 4
    assert len(out.map_paths) == 4


def test_step_disabled_returns_empty(tmp_path: Path):
    """enabled=false short-circuits — no gen_plot calls, no error."""
    geojson_base = tmp_path / "geojsons"
    (geojson_base / "mandals").mkdir(parents=True)
    ctx = _make_ctx(tmp_path, geojson_base)
    ctx.config["downscale_maps"] = {"enabled": False}
    # No predictions written: confirms the step short-circuits BEFORE reading.
    ds_result = DownscaleResult(
        output_csv_path=f"{ctx.run_id}/outputs/nope.csv",
        n_parent_rows=0,
        n_child_rows=0,
    )
    with patch(
        "pipelines.dengue_downscale.steps.generate_downscale_maps.maps.gen_plot"
    ) as gen:
        out = GenerateDownscaleMapsStep().run(
            ctx, GenerateDownscaleMapsInputs(downscale_predictions=ds_result)
        )
    assert out.map_paths == ()
    gen.assert_not_called()


def test_step_handles_empty_predictions(tmp_path: Path):
    """Empty CSV → warn + return ()."""
    geojson_base = tmp_path / "geojsons"
    (geojson_base / "mandals").mkdir(parents=True)
    ctx = _make_ctx(tmp_path, geojson_base)
    key = f"{ctx.run_id}/outputs/predictions.csv"
    ctx.artifacts.write_text(
        "regionID,startDatePredictedWeek,model,thresholdMethod,prediction,predictionZone\n",
        key,
    )
    ds_result = DownscaleResult(output_csv_path=key, n_parent_rows=0, n_child_rows=0)
    with patch(
        "pipelines.dengue_downscale.steps.generate_downscale_maps.maps.gen_plot"
    ) as gen:
        out = GenerateDownscaleMapsStep().run(
            ctx, GenerateDownscaleMapsInputs(downscale_predictions=ds_result)
        )
    assert out.map_paths == ()
    gen.assert_not_called()


def test_step_raises_on_missing_geojson_base(tmp_path: Path):
    """A nonexistent geojson_base_path must fail loudly, not silently emit zero maps."""
    ctx = _make_ctx(tmp_path, tmp_path / "does_not_exist")
    pred_key = _make_predictions_csv(ctx.artifacts, ctx.run_id)
    ds_result = DownscaleResult(
        output_csv_path=pred_key, n_parent_rows=2, n_child_rows=4
    )
    with pytest.raises(FileNotFoundError, match="geojson base"):
        GenerateDownscaleMapsStep().run(
            ctx, GenerateDownscaleMapsInputs(downscale_predictions=ds_result)
        )


def test_step_passes_child_level_as_region(tmp_path: Path):
    """gen_plot's region arg must come from downscale.child_level, not parent_level."""
    geojson_base = tmp_path / "geojsons"
    (geojson_base / "mandals").mkdir(parents=True)
    ctx = _make_ctx(tmp_path, geojson_base)
    pred_key = _make_predictions_csv(ctx.artifacts, ctx.run_id)
    ds_result = DownscaleResult(
        output_csv_path=pred_key, n_parent_rows=2, n_child_rows=4
    )
    with patch(
        "pipelines.dengue_downscale.steps.generate_downscale_maps.maps.gen_plot",
        return_value="/tmp/fake.png",
    ) as gen:
        GenerateDownscaleMapsStep().run(
            ctx, GenerateDownscaleMapsInputs(downscale_predictions=ds_result)
        )
    # All calls used child_level="mandal".
    for call in gen.call_args_list:
        assert call.kwargs["region"] == "mandal"

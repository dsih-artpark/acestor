"""Regression test: ihip parser must skip files whose date columns have zero
parseable rows instead of raising and killing the whole prep pipeline.

Trigger: gba-ward-prep runs on Aug 19-21 2026 all failed on a residual
single-day dashboard export whose 3 rows had date strings that pandas on the
compute box could not parse. A 3-row residual should never sink the DAG."""

from __future__ import annotations

import logging
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Polygon

from pipelines.dengue_prep.lib.ihip import parse_ihip_files


def _write_geojson(base: Path, region_type: str) -> None:
    folder = base / f"{region_type}s"
    folder.mkdir(parents=True, exist_ok=True)
    poly = Polygon([(77.0, 12.0), (78.0, 12.0), (78.0, 13.0), (77.0, 13.0)])
    gdf = gpd.GeoDataFrame(
        {"region_id": [f"{region_type}_1"]},
        geometry=[poly],
        crs="EPSG:4326",
    )
    gdf.to_file(folder / "region_1.geojson", driver="GeoJSON")


def test_file_with_unparseable_dates_is_skipped_not_raised(tmp_path, caplog):
    case_dir = tmp_path / "cases"
    case_dir.mkdir()
    geo_base = tmp_path / "geo"
    _write_geojson(geo_base, "district")

    # One good file — should be aggregated normally.
    good = pd.DataFrame(
        {
            "Sample Collected Date": ["01/01/2024", "02/01/2024"],
            "Latitude": [12.5, 12.6],
            "Longitude": [77.5, 77.6],
        }
    )
    good.to_csv(case_dir / "good.csv", index=False)

    # One bad file — date column present but every row unparseable.
    bad = pd.DataFrame(
        {
            "Sample Collected Date": ["not-a-date", "also-not", "still-no"],
            "Latitude": [12.5, 12.6, 12.7],
            "Longitude": [77.5, 77.6, 77.7],
        }
    )
    bad.to_csv(case_dir / "bad.csv", index=False)

    with caplog.at_level(logging.WARNING):
        result = parse_ihip_files(
            folder=str(case_dir),
            region_type="district",
            geojson_base=str(geo_base),
        )

    # Good file's rows aggregated; bad file skipped, no crash.
    assert not result.empty
    assert result["case_count"].sum() == 2
    assert any(
        "no usable date column" in r.getMessage() and "skipping" in r.getMessage()
        for r in caplog.records
    ), "expected a WARNING log line naming the skipped file"


def test_all_files_unparseable_returns_empty_no_raise(tmp_path):
    case_dir = tmp_path / "cases"
    case_dir.mkdir()
    geo_base = tmp_path / "geo"
    _write_geojson(geo_base, "district")

    bad = pd.DataFrame(
        {
            "Sample Collected Date": ["not-a-date", "also-not"],
            "Latitude": [12.5, 12.6],
            "Longitude": [77.5, 77.6],
        }
    )
    bad.to_csv(case_dir / "only_bad.csv", index=False)

    # Should not raise. Downstream code is responsible for empty-frame handling.
    try:
        result = parse_ihip_files(
            folder=str(case_dir),
            region_type="district",
            geojson_base=str(geo_base),
        )
    except ValueError as exc:  # pragma: no cover - regression guard
        pytest.fail(f"parse_ihip_files raised on unparseable-only input: {exc}")
    assert result.empty

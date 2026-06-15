"""Regression test for issue #60: dengue_prep must drop rows with missing
coordinates before the spatial join instead of raising."""

from __future__ import annotations

import logging
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import Polygon

from pipelines.dengue_prep.lib.ihip import parse_ihip_files


def _write_geojson(base: Path, region_type: str) -> None:
    """Write a single-polygon geojson with a region_id property."""
    folder = base / f"{region_type}s"
    folder.mkdir(parents=True, exist_ok=True)
    poly = Polygon([(77.0, 12.0), (78.0, 12.0), (78.0, 13.0), (77.0, 13.0)])
    gdf = gpd.GeoDataFrame(
        {"region_id": [f"{region_type}_1"]},
        geometry=[poly],
        crs="EPSG:4326",
    )
    gdf.to_file(folder / "region_1.geojson", driver="GeoJSON")


def test_spatial_join_drops_rows_with_missing_coords(tmp_path, caplog):
    case_dir = tmp_path / "cases"
    case_dir.mkdir()
    geo_base = tmp_path / "geo"
    _write_geojson(geo_base, "district")

    # Two valid rows inside the polygon, two rows with missing coords.
    df = pd.DataFrame(
        {
            "Sample Collected Date": [
                "01/01/2024",
                "01/01/2024",
                "02/01/2024",
                "02/01/2024",
            ],
            "Latitude": [12.5, 12.6, None, 12.7],
            "Longitude": [77.5, 77.6, 77.5, None],
        }
    )
    df.to_csv(case_dir / "cases.csv", index=False)

    with caplog.at_level(logging.WARNING):
        result = parse_ihip_files(
            folder=str(case_dir),
            region_type="district",
            geojson_base=str(geo_base),
        )

    # Two rows dropped, two retained; aggregated into 1 (region, date) group
    # for 2024-01-01 with case_count == 2.
    assert not result.empty
    assert result["case_count"].sum() == 2
    assert (result["region_id"] == "district_1").all()
    assert any(
        "dropped 2 row(s) with missing" in record.getMessage()
        for record in caplog.records
    )


def test_spatial_join_drops_out_of_bounds_coords(tmp_path, caplog):
    case_dir = tmp_path / "cases"
    case_dir.mkdir()
    geo_base = tmp_path / "geo"
    _write_geojson(geo_base, "district")

    # First row inside polygon, second row far outside (~Delhi vs Bangalore area).
    df = pd.DataFrame(
        {
            "Sample Collected Date": ["01/01/2024", "01/01/2024"],
            "Latitude": [12.5, 28.6],
            "Longitude": [77.5, 77.2],
        }
    )
    df.to_csv(case_dir / "cases.csv", index=False)

    with caplog.at_level(logging.WARNING):
        result = parse_ihip_files(
            folder=str(case_dir),
            region_type="district",
            geojson_base=str(geo_base),
        )

    assert result["case_count"].sum() == 1
    assert any(
        "did not fall within any district polygon" in record.getMessage()
        for record in caplog.records
    )


def test_spatial_join_all_missing_coords_yields_empty(tmp_path, caplog):
    case_dir = tmp_path / "cases"
    case_dir.mkdir()
    geo_base = tmp_path / "geo"
    _write_geojson(geo_base, "district")

    df = pd.DataFrame(
        {
            "Sample Collected Date": ["01/01/2024", "02/01/2024"],
            "Latitude": [None, None],
            "Longitude": [None, None],
        }
    )
    df.to_csv(case_dir / "cases.csv", index=False)

    with caplog.at_level(logging.WARNING):
        result = parse_ihip_files(
            folder=str(case_dir),
            region_type="district",
            geojson_base=str(geo_base),
        )

    assert result.empty

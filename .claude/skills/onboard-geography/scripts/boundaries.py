"""Boundary files: check a user's file and write acestor's per-unit layout.

acestor reads one GeoJSON per unit from <base>/<level>s/<region_id>.geojson,
each carrying region_id, name and parent. Users usually arrive with a single
shapefile or GeoJSON and their own property names.
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import pandas as pd

from contracts import BOUNDARY_PROPERTIES
from qc import region_id
from report import FAIL, PASS, Check, failed

LAYOUT_CRS = "EPSG:4326"


def unit_ids(base: Path, region_type: str) -> set[str]:
    """Unit IDs present in the per-unit layout, read from file names."""
    for folder in (base / f"{region_type}s", base / region_type):
        files = list(folder.glob("*.geojson"))
        if files:
            return {file.stem for file in files}
    return set()


BOUNDARY_TYPES = (".geojson", ".shp")


def source_files(path: Path) -> list[Path]:
    """A boundary file, or every GeoJSON and shapefile in a folder."""
    if path.is_dir():
        return sorted(f for f in path.iterdir() if f.suffix.lower() in BOUNDARY_TYPES)
    return [path]


def read_boundaries(path: Path) -> gpd.GeoDataFrame:
    """One boundary file, or every boundary file in a folder put together."""
    files = source_files(path)
    if not files:
        raise FileNotFoundError(f"No {' or '.join(BOUNDARY_TYPES)} file in {path}.")
    parts = [gpd.read_file(file) for file in files]
    target = parts[0].crs
    return gpd.GeoDataFrame(pd.concat([part.to_crs(target) for part in parts],
                                      ignore_index=True), crs=target)


def check_source(source: gpd.GeoDataFrame, code_col: str, name_col: str) -> list[Check]:
    missing = [c for c in (code_col, name_col) if c not in source.columns]
    if missing:
        return [Check("boundary_columns", FAIL,
                      f"Columns not found: {missing}; have {list(source.columns)}.")]
    checks = [Check("boundary_columns", PASS, "Code and name columns found.")]
    codes = source[code_col].astype(str)
    blank = int((source[code_col].isna() | (codes.str.strip() == "")).sum())
    repeated = sorted(codes[codes.duplicated()].unique())
    checks.append(Check("boundary_codes_present", FAIL if blank else PASS,
                        f"{blank} units have no code." if blank
                        else "Every unit has a code."))
    checks.append(Check("boundary_codes_unique", FAIL if repeated else PASS,
                        f"Unit codes used more than once: {repeated}." if repeated
                        else "Unit codes are unique."))
    checks.append(Check("boundary_crs", FAIL if source.crs is None else PASS,
                        "The file has no coordinate reference system, so it cannot "
                        "be placed on the map." if source.crs is None
                        else f"Coordinate reference system: {source.crs.to_string()}."))
    broken = int((source.geometry.isna() | source.geometry.is_empty
                  | ~source.geometry.is_valid).sum())
    checks.append(Check("boundary_shapes_valid", FAIL if broken else PASS,
                        f"{broken} units have a missing or invalid shape." if broken
                        else "All shapes are valid."))
    other = sorted(set(source.geometry.geom_type.dropna()) - {"Polygon", "MultiPolygon"})
    checks.append(Check("boundary_shapes_are_polygons", FAIL if other else PASS,
                        f"Shapes must be polygons; found {other}." if other
                        else "All shapes are polygons."))
    return checks


def write_layout(source: gpd.GeoDataFrame, code_col: str, name_col: str,
                 region_type: str, parent_id: str, parent_name: str, base: Path) -> list[str]:
    """One file per unit, named by region_id, in WGS84."""
    folder = base / f"{region_type}s"
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    for _, row in source.to_crs(LAYOUT_CRS).iterrows():
        unit = region_id(region_type, row[code_col])
        feature = gpd.GeoDataFrame(
            {"region_id": [unit], "name": [str(row[name_col])],
             "parent": [parent_id], "parent_name": [parent_name]},
            geometry=[row.geometry], crs=LAYOUT_CRS,
        )
        feature.to_file(folder / f"{unit}.geojson", driver="GeoJSON")
        written.append(unit)
    return written


def check_layout(base: Path, region_type: str, expected: int) -> list[Check]:
    """Read the layout back: right number of files, each with the required properties."""
    ids = unit_ids(base, region_type)
    checks = [Check("layout_unit_count", PASS if len(ids) == expected else FAIL,
                    f"{len(ids)} unit files written, {expected} units in the source.")]
    wrong = []
    for unit in sorted(ids):
        feature = gpd.read_file(base / f"{region_type}s" / f"{unit}.geojson")
        missing = [p for p in BOUNDARY_PROPERTIES if p not in feature.columns]
        if missing or feature.region_id.iloc[0] != unit:
            wrong.append(unit)
    checks.append(Check("layout_properties", FAIL if wrong else PASS,
                        f"Files missing {list(BOUNDARY_PROPERTIES)} or misnamed: {wrong}."
                        if wrong else "Every file has region_id, name and parent, and "
                        "its name matches its region_id."))
    return checks


def prepare(input_file: Path, code_col: str, name_col: str, region_type: str,
            parent_id: str, parent_name: str, base: Path) -> tuple[dict, list[Check]]:
    source = read_boundaries(input_file)
    checks = check_source(source, code_col, name_col)
    info = {"file": str(input_file), "units_in_file": len(source)}
    if failed(checks):
        return info, checks
    written = write_layout(source, code_col, name_col, region_type, parent_id,
                           parent_name, base)
    checks.extend(check_layout(base, region_type, expected=len(source)))
    return {**info, "folder": str(base / f"{region_type}s"), "units": written}, checks

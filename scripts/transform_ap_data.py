"""
AP Data Transformation Script
==============================
Converts raw AP data (IHIP Excel + ERA5 wide CSVs + district shapefiles) into
the format expected by the acestor dengue pipeline.

Usage:
    python scripts/transform_ap_data.py --data-dir <path-to-raw-data> [--out-dir ap_datasets]

Steps executed:
    1. Case data  — IHIP Excel → ap_datasets/raw_linelist_data/AP_IHIP/ap_dengue_cases.csv
    2. Weather    — ERA5 wide CSVs → ap_datasets/parsednetcdf/district/YYYY/YYYY_MM.csv
    3. GeoJSON    — District shapefile → ap_datasets/geojsons/geojsons_AP/districts/<id>.geojson
                    (requires: pip install geopandas  AND  brew/apt install unrar)

After running this script, point your pipeline config to:
    data.case_download.source_path: ap_datasets/raw_linelist_data/AP_IHIP
    data.geojson.base_path:         ap_datasets/geojsons/geojsons_AP
    data.weather_download.parsed_output_path: ap_datasets/parsednetcdf
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
EXCEL_GLOB = "Lab Confirmed Cases*.xlsx"
ERA5_ZIP = "era5_data.zip"
ERA5_DIR = "extracted/era5_data"  # if already unzipped
BOUNDARIES_RAR = "28_Administrative Boundaries.rar"

ERA5_ID_COLS = ["state", "district", "mandal", "LGD_Code_d", "LGD_Code_m"]


# ---------------------------------------------------------------------------
# Step 1: Case data
# ---------------------------------------------------------------------------
def transform_case_data(data_dir: Path, out_dir: Path) -> Path:
    log.info("=== Step 1: Case data ===")

    excel_files = list(data_dir.glob(EXCEL_GLOB))
    if not excel_files:
        raise FileNotFoundError(f"No Excel file matching '{EXCEL_GLOB}' in {data_dir}")
    excel_path = excel_files[0]
    log.info("Reading: %s", excel_path.name)

    xl = pd.ExcelFile(excel_path)
    sheets = []
    for sheet in xl.sheet_names:
        df = pd.read_excel(excel_path, sheet_name=sheet)
        sheets.append(df)
        log.info("  Sheet '%s': %d rows", sheet, len(df))

    all_cases = pd.concat(sheets, ignore_index=True)
    log.info("Total rows (all diseases): %d", len(all_cases))

    # Filter to dengue only
    dengue = all_cases[all_cases["Confirmed Diagnosis"] == "Dengue"].copy()
    log.info("Dengue rows: %d", len(dengue))

    # Reformat date: DD/MM/YYYY → YYYY-MM-DD
    dengue["metadata.primaryDate"] = pd.to_datetime(
        dengue["Sample Collected Date"], dayfirst=True, errors="coerce"
    ).dt.strftime("%Y-%m-%d")

    # LGD codes — prefix with region type so pipeline region detection works
    # e.g. "district_502"  matches df["regionID"].str.startswith("district")
    dengue["location.admin2.ID"] = "district_" + dengue["District Code"].astype(
        "Int64"
    ).astype(str)
    dengue["location.admin3.ID"] = "subdistrict_" + dengue["Sub District Code"].astype(
        "Int64"
    ).astype(str)

    # Drop rows missing date or district
    before = len(dengue)
    dengue = dengue.dropna(subset=["metadata.primaryDate", "location.admin2.ID"])
    log.info(
        "Dropped %d rows with missing date/district; %d remain",
        before - len(dengue),
        len(dengue),
    )

    output_cols = [
        "metadata.primaryDate",
        "location.admin2.ID",
        "location.admin3.ID",
        "District Name",
        "Mandal Name",
    ]
    output = dengue[[c for c in output_cols if c in dengue.columns]]

    out_path = out_dir / "raw_linelist_data" / "AP_IHIP" / "ap_dengue_cases.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    output.to_csv(out_path, index=False)
    log.info("Saved case data → %s (%d rows)", out_path, len(output))
    return out_path


# ---------------------------------------------------------------------------
# Step 2: Weather data
# ---------------------------------------------------------------------------
def _read_era5_csv(era5_dir: Path, filename: str) -> pd.DataFrame:
    path = era5_dir / filename
    if not path.exists():
        raise FileNotFoundError(f"ERA5 file not found: {path}")
    df = pd.read_csv(path)
    # Drop unnamed index column
    df = df.loc[:, ~df.columns.str.startswith("Unnamed")]
    return df


_DATE_COL_RE = re.compile(r"^mean\.X\d{4}\.\d{2}\.\d{2}$")


def _melt_to_long(df: pd.DataFrame, value_name: str) -> pd.DataFrame:
    # Exclude duplicate-suffixed cols like mean.X2022.01.31.1 (pandas deduplication artefact)
    date_cols = [c for c in df.columns if _DATE_COL_RE.match(c)]
    id_cols = [c for c in ERA5_ID_COLS if c in df.columns]
    melted = df[id_cols + date_cols].melt(
        id_vars=id_cols, var_name="date_raw", value_name=value_name
    )
    # Parse "mean.X2022.01.01" → "2022-01-01"
    melted["date"] = pd.to_datetime(
        melted["date_raw"]
        .str.replace("mean.X", "", regex=False)
        .str.replace(".", "-", regex=False),
        format="%Y-%m-%d",
    )
    return melted.drop(columns=["date_raw"])


def _dewpoint_from_temp_rh(temp_c: pd.Series, rh_pct: pd.Series) -> pd.Series:
    """Magnus formula: dewpoint from temperature (°C) + relative humidity (%)."""
    a, b = 17.27, 237.7
    alpha = (a * temp_c / (b + temp_c)) + np.log(rh_pct / 100.0)
    return (b * alpha) / (a - alpha)


def transform_weather_data(data_dir: Path, out_dir: Path) -> Path:
    log.info("=== Step 2: Weather data ===")

    # Prefer already-extracted directory
    era5_dir = data_dir / ERA5_DIR
    if not era5_dir.exists():
        # Try extracting from zip
        zip_path = data_dir / ERA5_ZIP
        if not zip_path.exists():
            raise FileNotFoundError(
                f"ERA5 data not found. Expected either:\n"
                f"  {era5_dir}  (extracted directory)\n"
                f"  {zip_path}  (zip archive)"
            )
        log.info("Extracting ERA5 zip to %s ...", era5_dir)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(era5_dir)

    log.info("Loading ERA5 files from %s", era5_dir)
    temp_min = _melt_to_long(
        _read_era5_csv(era5_dir, "temperature_min_deg.csv"), "t2m_min"
    )
    temp_max = _melt_to_long(
        _read_era5_csv(era5_dir, "temperature_max_deg.csv"), "t2m_max"
    )
    rh_mean = _melt_to_long(
        _read_era5_csv(era5_dir, "relative_humidity_daymean.csv"), "rh_mean"
    )
    rainfall = _melt_to_long(_read_era5_csv(era5_dir, "era5_rainfall_mm.csv"), "tp")
    log.info("Loaded 4 ERA5 variables (%d mandal-days each)", len(temp_min))

    # Mean temperature = (min + max) / 2
    merge_keys = [c for c in ERA5_ID_COLS if c in temp_min.columns] + ["date"]
    base = temp_min.merge(temp_max, on=merge_keys, suffixes=("", "_max"))
    base["t2m"] = (base["t2m_min"] + base["t2m_max"]) / 2

    base = base.merge(rh_mean, on=merge_keys, how="left")
    base = base.merge(rainfall, on=merge_keys, how="left")

    # Compute dewpoint from mean temperature + mean RH
    base["d2m"] = _dewpoint_from_temp_rh(base["t2m"], base["rh_mean"])

    # Aggregate mandal → district (mean t2m, mean d2m, sum tp)
    district = base.groupby(
        ["LGD_Code_d", "district", "state", "date"], as_index=False
    ).agg({"t2m": "mean", "d2m": "mean", "tp": "sum"})

    district.rename(
        columns={
            "LGD_Code_d": "region_id",
            "district": "name",
            "state": "parent_name",
        },
        inplace=True,
    )

    # Drop rows with invalid LGD code (e.g. Yanam appears as 0 in ERA5 data)
    before = len(district)
    district = district[district["region_id"] > 0]
    if len(district) < before:
        log.warning(
            "Dropped %d rows with region_id=0 (invalid LGD code)",
            before - len(district),
        )

    # Prefix with region type so pipeline region detection works
    # e.g. "district_502" matches df["regionID"].str.startswith("district")
    district["region_id"] = "district_" + district["region_id"].astype(str)
    district["parent"] = "28"  # AP state LGD code
    district["time"] = district["date"].astype(str) + " 00:00:00"

    output_cols = [
        "time",
        "t2m",
        "d2m",
        "tp",
        "region_id",
        "name",
        "parent",
        "parent_name",
    ]
    district = district[output_cols]

    log.info(
        "District-level rows: %d across %d districts",
        len(district),
        district["region_id"].nunique(),
    )

    # Write one CSV per month per year
    out_base = out_dir / "parsednetcdf" / "district"
    district["_date"] = pd.to_datetime(district["time"])
    written = 0
    for (year, month), group in district.groupby(
        [district["_date"].dt.year, district["_date"].dt.month]
    ):
        month_dir = out_base / str(year)
        month_dir.mkdir(parents=True, exist_ok=True)
        out_path = month_dir / f"{year}_{month:02d}.csv"
        group.drop(columns=["_date"]).to_csv(out_path, index=False)
        written += 1

    log.info("Written %d monthly CSVs → %s", written, out_base)
    return out_base


# ---------------------------------------------------------------------------
# Step 3: GeoJSON from shapefile
# ---------------------------------------------------------------------------
def transform_geojson(
    data_dir: Path, out_dir: Path, shp_path: Path | None = None
) -> Path:
    log.info("=== Step 3: GeoJSON boundaries ===")

    try:
        import geopandas as gpd
    except ImportError:
        log.error(
            "geopandas not installed. Run: pip install geopandas\n"
            "Then re-run this script with --steps geojson"
        )
        raise

    if shp_path is None:
        # Try to find the extracted shapefile
        candidates = list(data_dir.rglob("*.shp"))
        if not candidates:
            log.error(
                "No .shp file found under %s\n"
                "Extract the RAR archive first:\n"
                "  brew install rar && unrar x '28_Administrative Boundaries.rar' /tmp/ap_boundaries/\n"
                "Then run with: --shp /tmp/ap_boundaries/AP_Districts.shp",
                data_dir,
            )
            raise FileNotFoundError("No shapefile found. See instructions above.")
        shp_path = candidates[0]
        log.info("Found shapefile: %s", shp_path)

    gdf = gpd.read_file(shp_path).to_crs(epsg=4326)
    log.info("Shapefile columns: %s", gdf.columns.tolist())

    # Auto-detect LGD district code column
    lgd_col = next(
        (
            c
            for c in gdf.columns
            if c.lower()
            in ("dis_lgd", "lgd_code_d", "lgd_code", "dist_code", "lgdcode")
        ),
        None,
    )
    name_col = next(
        (
            c
            for c in gdf.columns
            if c.lower() in ("district", "dist_name", "name_2", "district_name")
        ),
        None,
    )
    if lgd_col is None or name_col is None:
        log.error(
            "Could not auto-detect LGD/name columns. Found: %s\n"
            "Edit the LGD_COL / NAME_COL values at the top of this function.",
            gdf.columns.tolist(),
        )
        raise ValueError("Could not detect LGD district code column in shapefile")

    log.info("Using LGD column: '%s', name column: '%s'", lgd_col, name_col)

    import json

    out_geojson_dir = out_dir / "geojsons" / "geojsons_AP" / "districts"
    out_geojson_dir.mkdir(parents=True, exist_ok=True)

    count = 0
    skipped = 0
    for _, row in gdf.iterrows():
        try:
            region_id = f"district_{int(row[lgd_col])}"
        except (ValueError, TypeError):
            log.warning(
                "Skipping row with invalid LGD code: %s=%r", lgd_col, row[lgd_col]
            )
            skipped += 1
            continue
        feature = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        "region_id": region_id,
                        "name": str(row[name_col]),
                        "parent": "28",
                        "parent_name": "Andhra Pradesh",
                    },
                    "geometry": row["geometry"].__geo_interface__,
                }
            ],
        }
        out_path = out_geojson_dir / f"{region_id}.geojson"
        with open(out_path, "w") as f:
            json.dump(feature, f)
        count += 1

    if skipped:
        log.warning("Skipped %d rows with invalid LGD codes", skipped)
    log.info("Written %d GeoJSON files → %s", count, out_geojson_dir)
    return out_geojson_dir


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate(out_dir: Path) -> None:
    log.info("=== Validation ===")

    case_csv = out_dir / "raw_linelist_data" / "AP_IHIP" / "ap_dengue_cases.csv"
    if case_csv.exists():
        df = pd.read_csv(case_csv)
        log.info(
            "Case data: %d rows, %d districts, date range %s → %s",
            len(df),
            df["location.admin2.ID"].nunique(),
            df["metadata.primaryDate"].min(),
            df["metadata.primaryDate"].max(),
        )
        assert "metadata.primaryDate" in df.columns
        assert "location.admin2.ID" in df.columns
        log.info("  ✓ Case data OK")
    else:
        log.warning("  ✗ Case data not found at %s", case_csv)

    weather_dir = out_dir / "parsednetcdf" / "district"
    weather_files = list(weather_dir.rglob("*.csv")) if weather_dir.exists() else []
    if weather_files:
        sample = pd.read_csv(weather_files[0])
        expected = {"time", "t2m", "d2m", "tp", "region_id", "name"}
        missing = expected - set(sample.columns)
        if missing:
            log.warning("  ✗ Weather CSVs missing columns: %s", missing)
        else:
            log.info("  ✓ Weather data OK (%d monthly files)", len(weather_files))
    else:
        log.warning("  ✗ Weather data not found at %s", weather_dir)

    geojson_dir = out_dir / "geojsons" / "geojsons_AP" / "districts"
    geojson_files = list(geojson_dir.glob("*.geojson")) if geojson_dir.exists() else []
    if geojson_files:
        log.info("  ✓ GeoJSON files: %d districts", len(geojson_files))
    else:
        log.warning(
            "  ✗ GeoJSON files not found at %s\n"
            "    Run step 3 after installing geopandas + unrar",
            geojson_dir,
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Transform AP raw data for acestor pipeline"
    )
    parser.add_argument(
        "--data-dir",
        default="drive-download-20260327T104821Z-3-001",
        help="Path to the raw AP data folder (default: drive-download-20260327T104821Z-3-001)",
    )
    parser.add_argument(
        "--out-dir",
        default="ap_datasets",
        help="Output directory (default: ap_datasets)",
    )
    parser.add_argument(
        "--steps",
        nargs="+",
        choices=["case", "weather", "geojson", "all"],
        default=["all"],
        help="Which steps to run (default: all)",
    )
    parser.add_argument(
        "--shp",
        default=None,
        help="Path to district shapefile (for geojson step, if extracted manually)",
    )
    args = parser.parse_args()

    data_dir = Path(args.data_dir).resolve()
    out_dir = Path(args.out_dir).resolve()
    steps = set(args.steps)
    run_all = "all" in steps

    if not data_dir.exists():
        log.error("Data directory not found: %s", data_dir)
        sys.exit(1)

    out_dir.mkdir(parents=True, exist_ok=True)
    log.info("Data dir: %s", data_dir)
    log.info("Output dir: %s", out_dir)

    errors = []

    if run_all or "case" in steps:
        try:
            transform_case_data(data_dir, out_dir)
        except Exception as e:
            log.error("Case data step failed: %s", e)
            errors.append("case")

    if run_all or "weather" in steps:
        try:
            transform_weather_data(data_dir, out_dir)
        except Exception as e:
            log.error("Weather step failed: %s", e)
            errors.append("weather")

    if run_all or "geojson" in steps:
        shp_path = Path(args.shp) if args.shp else None
        try:
            transform_geojson(data_dir, out_dir, shp_path)
        except Exception as e:
            log.warning("GeoJSON step skipped/failed: %s", e)
            # Not added to errors — it's a known blocker (unrar required)

    validate(out_dir)

    if errors:
        log.error("Failed steps: %s", errors)
        sys.exit(1)
    else:
        log.info("Done. Now run the pipeline:")
        log.info(
            "  uv run python -m acestor.run "
            "--pipeline pipelines.dengue.pipeline:build_pipeline "
            "--config configs/ap_district.yaml"
        )


if __name__ == "__main__":
    main()

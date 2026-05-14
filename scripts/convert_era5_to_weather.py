"""Convert ERA5 wide-format CSVs → district and mandal daily per-month CSVs.

Usage:
    python scripts/convert_era5_to_weather.py <era5_dir> [output_base_dir]

    era5_dir        — folder containing ERA5 wide-format CSVs:
                        era5_rainfall_mm.csv
                        temperature_max_deg.csv
                        temperature_min_deg.csv
                        relative_humidity_daymean.csv
    output_base_dir — root output folder (default: ap_datasets/weather)

Outputs (written under output_base_dir):
    district/YYYY/YYYY_MM.csv   — one row per district per day
    mandal/YYYY/YYYY_MM.csv     — one row per mandal per day

Column schema (same as download step output):
    date, region_id, t2m, d2m, tp, name, parent, parent_name

Units (normalized, same as what the pipeline download step produces):
    t2m, d2m  — Kelvin   (ERA5 °C + 273.15)
    tp        — metres   (ERA5 mm / 1000)

Aggregation (district level):
    t2m, d2m  — spatial mean across mandals in the district
    tp        — spatial mean (rainfall is areal average, not a sum)

Region ID:
    district  — LGD_Code_d  (e.g. 515)
    mandal    — LGD_Code_m  (e.g. 4887)

Existing files in the output folders are overwritten.
Months not covered by the ERA5 files are left untouched.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if len(sys.argv) < 2:
    print(__doc__)
    sys.exit(1)

ERA5_DIR = Path(sys.argv[1])
OUTPUT_BASE = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("ap_datasets/weather")

if not ERA5_DIR.is_dir():
    print(f"ERROR: era5_dir not found: {ERA5_DIR}")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Load ERA5 files
# ---------------------------------------------------------------------------
print(f"Loading ERA5 files from {ERA5_DIR} ...")
rain = pd.read_csv(ERA5_DIR / "era5_rainfall_mm.csv")
t_max = pd.read_csv(ERA5_DIR / "temperature_max_deg.csv")
t_min = pd.read_csv(ERA5_DIR / "temperature_min_deg.csv")
rh = pd.read_csv(ERA5_DIR / "relative_humidity_daymean.csv")

META_COLS = [
    "state",
    "district",
    "mandal",
    "LGD_Code_d",
    "LGD_Code_m",
    "Sate_UUID",
    "Dist_UUID",
    "Mand_UUID",
]
META_PRESENT = [c for c in META_COLS if c in rain.columns]


# ---------------------------------------------------------------------------
# Parse date column names  →  Timestamps
# Handles duplicate suffixes like mean.X2022.01.31.1
# ---------------------------------------------------------------------------
def _parse_date_col(col: str) -> pd.Timestamp | None:
    raw = col.removeprefix("mean.X")
    parts = raw.split(".")
    if len(parts) == 4:
        parts = parts[:3]
    try:
        return pd.Timestamp(".".join(parts))
    except Exception:
        return None


col_to_date: dict[str, pd.Timestamp] = {}
seen: set[pd.Timestamp] = set()
for col in rain.columns:
    if not col.startswith("mean.X"):
        continue
    dt = _parse_date_col(col)
    if dt is None or dt in seen:
        continue
    col_to_date[col] = dt
    seen.add(dt)

date_cols = list(col_to_date.keys())
print(
    f"  {len(date_cols)} unique dates: "
    f"{min(col_to_date.values()).date()} → {max(col_to_date.values()).date()}"
)
print(f"  {len(rain)} mandal rows")


# ---------------------------------------------------------------------------
# Melt wide → long  (one value per mandal per date)
# ---------------------------------------------------------------------------
_JOIN_KEYS = ["LGD_Code_d", "LGD_Code_m", "district", "mandal", "state"]


def _melt(df: pd.DataFrame, value_name: str) -> pd.DataFrame:
    keep: list[str] = []
    s: set[pd.Timestamp] = set()
    for col in df.columns:
        if not col.startswith("mean.X"):
            continue
        dt = _parse_date_col(col)
        if dt is not None and dt not in s and col in col_to_date:
            keep.append(col)
            s.add(dt)

    id_cols = [c for c in _JOIN_KEYS if c in df.columns]
    long = df[id_cols + keep].melt(
        id_vars=id_cols, var_name="_col", value_name=value_name
    )
    long["date"] = long["_col"].map(col_to_date)
    return long[id_cols + ["date", value_name]]


print("Melting to long format ...")
df_rain = _melt(rain, "tp_mm")
df_tmax = _melt(t_max, "t_max_c")
df_tmin = _melt(t_min, "t_min_c")
df_rh = _melt(rh, "rh")

# ---------------------------------------------------------------------------
# Merge and derive variables
# ---------------------------------------------------------------------------
join_keys = [c for c in _JOIN_KEYS if c in df_rain.columns] + ["date"]

merged = (
    df_rain.merge(df_tmax, on=join_keys)
    .merge(df_tmin, on=join_keys)
    .merge(df_rh, on=join_keys)
)

# t2m: mean of daily max and min (°C)
merged["t2m_c"] = (merged["t_max_c"] + merged["t_min_c"]) / 2.0

# d2m: August-Roche-Magnus formula from temperature + relative humidity
rh_safe = np.clip(merged["rh"].values, 0.01, 100.0)
gamma = np.log(rh_safe / 100.0) + (17.625 * merged["t2m_c"].values) / (
    243.04 + merged["t2m_c"].values
)
merged["d2m_c"] = (243.04 * gamma) / (17.625 - gamma)

print(f"  Mandal-day rows: {len(merged):,}")


# ---------------------------------------------------------------------------
# Build mandal-level output (unit-converted)
# ---------------------------------------------------------------------------
def _to_kelvin(series: pd.Series) -> pd.Series:
    return series + 273.15


def _to_metres(series: pd.Series) -> pd.Series:
    return series / 1000.0


mandal_df = merged[
    ["date", "LGD_Code_m", "t2m_c", "d2m_c", "tp_mm", "mandal", "district", "state"]
].copy()
mandal_df["region_id"] = "mandal_" + mandal_df["LGD_Code_m"].astype(str)
mandal_df["t2m"] = _to_kelvin(mandal_df["t2m_c"])
mandal_df["d2m"] = _to_kelvin(mandal_df["d2m_c"])
mandal_df["tp"] = _to_metres(mandal_df["tp_mm"])
mandal_df["name"] = mandal_df["mandal"]
mandal_df["parent"] = mandal_df["district"]
mandal_df["parent_name"] = mandal_df["state"]
mandal_df = mandal_df[
    ["date", "region_id", "t2m", "d2m", "tp", "name", "parent", "parent_name"]
]


# ---------------------------------------------------------------------------
# Build district-level output (aggregate mandals → district, then unit-convert)
# ---------------------------------------------------------------------------
district_meta = (
    merged[["LGD_Code_d", "district", "state"]]
    .drop_duplicates("LGD_Code_d")
    .set_index("LGD_Code_d")
)

agg = merged.groupby(["LGD_Code_d", "date"], as_index=False).agg(
    t2m_c=("t2m_c", "mean"), d2m_c=("d2m_c", "mean"), tp_mm=("tp_mm", "mean")
)

district_df = agg.copy()
district_df = district_df[district_df["LGD_Code_d"] != 0]  # drop Yanam (LGD 0, invalid)
district_df["region_id"] = "district_" + district_df["LGD_Code_d"].astype(str)
district_df["t2m"] = _to_kelvin(district_df["t2m_c"])
district_df["d2m"] = _to_kelvin(district_df["d2m_c"])
district_df["tp"] = _to_metres(district_df["tp_mm"])
district_df["name"] = district_df["LGD_Code_d"].map(district_meta["district"])
district_df["parent"] = district_df["LGD_Code_d"].map(district_meta["state"])
district_df["parent_name"] = district_df["parent"]
district_df = district_df[
    ["date", "region_id", "t2m", "d2m", "tp", "name", "parent", "parent_name"]
]


# ---------------------------------------------------------------------------
# Write per-month CSVs for a given long dataframe
# ---------------------------------------------------------------------------
def _write_monthly(df: pd.DataFrame, out_dir: Path, label: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    df = df.copy()
    df["_year"] = df["date"].dt.year
    df["_month"] = df["date"].dt.month
    df["date"] = df["date"].dt.strftime("%Y-%m-%d")
    df = df.sort_values(["region_id", "date"]).reset_index(drop=True)

    count = 0
    for (year, month), grp in df.groupby(["_year", "_month"]):
        year_dir = out_dir / str(int(year))
        year_dir.mkdir(parents=True, exist_ok=True)
        dest = year_dir / f"{int(year)}_{int(month):02d}.csv"
        grp.drop(columns=["_year", "_month"]).to_csv(dest, index=False)
        count += 1

    print(f"  [{label}] wrote {count} monthly CSVs → {out_dir}/")


print("\nWriting district CSVs ...")
_write_monthly(district_df, OUTPUT_BASE / "district", "district")

print("Writing mandal CSVs ...")
_write_monthly(mandal_df, OUTPUT_BASE / "mandal", "mandal")

print(f"\nDone. Output base: {OUTPUT_BASE.resolve()}")
print(
    "NOTE: 2021-09 to 2021-11 are not in ERA5 (starts 2021-12-31). "
    "Those existing files were left untouched."
)

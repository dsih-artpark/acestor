"""Convert local ERA5 wide-format CSVs → per-month OpenMeteo-format CSVs.

Usage:
    python scripts/convert_era5_to_openmeteo_format.py <era5_dir> [output_dir]

    era5_dir   — directory containing ERA5 CSV files:
                   era5_rainfall_mm.csv, temperature_max_deg.csv,
                   temperature_min_deg.csv, relative_humidity_daymean.csv
    output_dir — destination root (default: datasets/openmeteo_ap/mandal)

Input format:  Wide — rows=mandals, columns=dates (mean.X2022.01.01 ...)
Output format: Long — time, t2m, d2m, tp, region_id, name, parent, parent_name
               Units: °C and mm  (pipeline config has convert_units: true → converts to K/m)

Dew point derived from relative humidity + temperature via August-Roche-Magnus formula.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Paths — from CLI args or defaults
# ---------------------------------------------------------------------------
if len(sys.argv) < 2:
    print(__doc__)
    sys.exit(1)

ERA5_DIR = Path(sys.argv[1])
OUTPUT_DIR = (
    Path(sys.argv[2]) if len(sys.argv) > 2 else Path("datasets/openmeteo_ap/mandal")
)

if not ERA5_DIR.is_dir():
    print(f"ERROR: era5_dir not found: {ERA5_DIR}")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Load all ERA5 files
# ---------------------------------------------------------------------------
print("Loading ERA5 files...")

rain = pd.read_csv(ERA5_DIR / "era5_rainfall_mm.csv")
t_max = pd.read_csv(ERA5_DIR / "temperature_max_deg.csv")
t_min = pd.read_csv(ERA5_DIR / "temperature_min_deg.csv")
rh_mean = pd.read_csv(ERA5_DIR / "relative_humidity_daymean.csv")

# ---------------------------------------------------------------------------
# Identify metadata columns vs date columns
# ---------------------------------------------------------------------------
META_COLS = [
    "Unnamed: 0",
    "OBJECTID",
    "state",
    "district",
    "mandal",
    "Sate_UUID",
    "Dist_UUID",
    "Mand_UUID",
    "LGD_Code_d",
    "LGD_Code_m",
    "UUID",
    "Area_Sqkm",
    "LGD_MAN",
    "Sec_Code",
]
META_PRESENT = [c for c in META_COLS if c in rain.columns]

date_cols = [c for c in rain.columns if c.startswith("mean.X")]
print(f"  {len(date_cols)} date columns: {date_cols[0]} → {date_cols[-1]}")
print(f"  {len(rain)} mandals")


# ---------------------------------------------------------------------------
# Parse date column names → actual dates
# Handle "mean.X2022.01.31.1" / "mean.X2022.01.31.2" duplicates
# ---------------------------------------------------------------------------
def parse_date_col(col: str) -> pd.Timestamp | None:
    """Convert 'mean.X2022.01.01' → Timestamp. Ignores .1/.2 suffix duplicates."""
    raw = col.removeprefix("mean.X")
    # drop trailing .1 .2 etc
    parts = raw.split(".")
    if len(parts) == 4:
        parts = parts[:3]
    try:
        return pd.Timestamp(".".join(parts))
    except Exception:
        return None


col_to_date: dict[str, pd.Timestamp] = {}
seen_dates: set[pd.Timestamp] = set()
for col in date_cols:
    dt = parse_date_col(col)
    if dt is None or dt in seen_dates:
        continue
    col_to_date[col] = dt
    seen_dates.add(dt)

valid_cols = list(col_to_date.keys())
print(f"  {len(valid_cols)} unique dates after deduplication")

# ---------------------------------------------------------------------------
# Build region metadata lookup: LGD_Code_m → (region_id, name, parent, parent_name)
# ---------------------------------------------------------------------------
meta = rain[META_PRESENT].copy()
meta["region_id"] = "mandal_" + meta["LGD_Code_m"].astype(int).apply(
    lambda x: f"{x:05d}"
)
meta["name"] = meta.get("mandal", pd.Series("", index=meta.index))
meta["parent"] = meta.get("Dist_UUID", pd.Series("", index=meta.index))
meta["parent_name"] = meta.get("district", pd.Series("", index=meta.index))
meta = meta[["LGD_Code_m", "region_id", "name", "parent", "parent_name"]].set_index(
    "LGD_Code_m"
)


# ---------------------------------------------------------------------------
# Magnus formula: dew point from temperature (°C) and relative humidity (%)
# ---------------------------------------------------------------------------
def rh_to_dewpoint(T: np.ndarray, RH: np.ndarray) -> np.ndarray:
    """August-Roche-Magnus approximation. T in °C, RH in %. Returns d2m in °C."""
    a, b = 17.625, 243.04
    RH_safe = np.clip(RH, 0.01, 100.0)
    gamma = np.log(RH_safe / 100.0) + (a * T) / (b + T)
    return (b * gamma) / (a - gamma)


# ---------------------------------------------------------------------------
# Melt each file into long format, compute derived variables
# ---------------------------------------------------------------------------
def melt_wide(df: pd.DataFrame, value_name: str) -> pd.DataFrame:
    """Melt wide DataFrame (rows=mandals, cols=dates) → long DataFrame.

    Handles per-file column differences: only uses date cols present in this file
    and not already seen (deduplicates .1/.2 suffixed duplicates).
    """
    file_date_cols = [c for c in df.columns if c.startswith("mean.X")]
    # Deduplicate: only keep first occurrence of each date
    seen: set[pd.Timestamp] = set()
    keep_cols: list[str] = []
    for col in file_date_cols:
        dt = parse_date_col(col)
        if dt is not None and dt not in seen:
            keep_cols.append(col)
            seen.add(dt)

    meta_in_file = [c for c in META_PRESENT if c in df.columns]
    sub = df[meta_in_file + keep_cols].copy()
    sub["region_id"] = "mandal_" + sub["LGD_Code_m"].astype(int).apply(
        lambda x: f"{x:05d}"
    )
    sub = sub[["region_id"] + keep_cols]
    long = sub.melt(id_vars="region_id", var_name="date_col", value_name=value_name)
    long["time"] = long["date_col"].map({c: parse_date_col(c) for c in keep_cols})
    return long[["region_id", "time", value_name]]


print("Melting files to long format...")
df_tp = melt_wide(rain, "tp")
df_tmax = melt_wide(t_max, "t_max")
df_tmin = melt_wide(t_min, "t_min")
df_rh = melt_wide(rh_mean, "rh")

# Merge all variables
combined = (
    df_tp.merge(df_tmax, on=["region_id", "time"])
    .merge(df_tmin, on=["region_id", "time"])
    .merge(df_rh, on=["region_id", "time"])
)

# Compute t2m (mean temperature) and d2m (dew point)
combined["t2m"] = (combined["t_max"] + combined["t_min"]) / 2.0
combined["d2m"] = rh_to_dewpoint(combined["t2m"].values, combined["rh"].values)

# Add metadata
combined = combined.merge(
    meta.reset_index()[["region_id", "name", "parent", "parent_name"]],
    on="region_id",
    how="left",
)
combined["name"] = combined["name"].fillna("")
combined["parent"] = combined["parent"].fillna("")
combined["parent_name"] = combined["parent_name"].fillna("")

# Final column order matching openmeteo.py output
combined = combined[
    ["time", "t2m", "d2m", "tp", "region_id", "name", "parent", "parent_name"]
]
combined = combined.sort_values(["region_id", "time"]).reset_index(drop=True)

print(f"Combined shape: {combined.shape}")
print(f"Date range: {combined['time'].min()} → {combined['time'].max()}")
print(f"Regions: {combined['region_id'].nunique()}")
print(f"Sample:\n{combined.head(3).to_string()}")

# ---------------------------------------------------------------------------
# Write per-month CSVs
# ---------------------------------------------------------------------------
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

combined["year"] = combined["time"].dt.year
combined["month"] = combined["time"].dt.month

months_written = 0
for (year, month), grp in combined.groupby(["year", "month"]):
    year_dir = OUTPUT_DIR / str(year)
    year_dir.mkdir(parents=True, exist_ok=True)
    out = year_dir / f"{year}_{month:02d}.csv"
    grp = grp.drop(columns=["year", "month"])
    grp.to_csv(out, index=False)
    months_written += 1

print(f"\nWrote {months_written} monthly CSVs to {OUTPUT_DIR}/")
print(f"Coverage: {combined['time'].min().date()} → {combined['time'].max().date()}")
print()
print("GAPS (need separate download or will be skipped by pipeline):")
print(
    "  - 2021-09-01 → 2021-12-31  (4 months, ~1,800 API units — fits in 1 hour limit)"
)
print("  - 2026-03-01 → present     (2 months, ~900 API units — fits in 1 hour limit)")
print("  - 8 mandals missing from ERA5 files (check region_id coverage)")
print()
print("Missing mandals from ERA5 vs probe script list:")
era5_ids = set(combined["region_id"].unique())
probe_ids = {
    "mandal_06063",
    "mandal_06833",
    "mandal_06836",
    "mandal_06837",
    "mandal_06838",
    "mandal_06839",
    "mandal_06863",
    "mandal_06864",
    "mandal_06865",
    "mandal_07182",
    "mandal_07183",
    "mandal_07425",
    "mandal_07426",
    "mandal_07427",
    "mandal_07428",
    "mandal_07429",
    "mandal_07430",
    "mandal_07431",
    "mandal_07562",
    "mandal_07563",
}
missing_from_era5 = probe_ids - era5_ids
print(f"  Probe-only mandals missing from ERA5: {sorted(missing_from_era5)}")

import argparse
import os
import re
from pathlib import Path

import geopandas as gpd
import pandas as pd
from geopy.geocoders import GoogleV3


def load_dotenv_fallback() -> None:
    try:
        from dotenv import load_dotenv
    except ModuleNotFoundError:
        env_path = Path(".env")
        if not env_path.exists():
            return
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))
    else:
        load_dotenv()


def slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").lower()
    return slug or "region"


def flatten_paths(values: list[list[Path]]) -> list[Path]:
    return [path for group in values for path in group]


def flatten_values(values: list[list[str]]) -> list[str]:
    return [value for group in values for value in group]


def parse_address_column(value: str) -> tuple[str, str | None]:
    if "=" in value:
        column, prefix = value.split("=", 1)
        column = column.strip()
        prefix = prefix.strip()
    else:
        column = value.strip()
        prefix = None

    if not column:
        raise argparse.ArgumentTypeError(
            "Address columns must be COLUMN or COLUMN=prefix."
        )
    if prefix == "":
        raise argparse.ArgumentTypeError(
            "Address column prefixes must not be empty."
        )
    return column, prefix


def address_configs_from_args(
    values: list[str],
    default_prefix: str,
) -> list[dict[str, str]]:
    parsed = [parse_address_column(value) for value in values]
    configs = []
    use_default_prefix = len(parsed) == 1 and parsed[0][1] is None

    for column, prefix in parsed:
        if prefix is None:
            prefix = default_prefix if use_default_prefix else slugify(column)
        configs.append({"column": column, "prefix": slugify(prefix)})

    prefixes = [config["prefix"] for config in configs]
    duplicate_prefixes = sorted(
        {prefix for prefix in prefixes if prefixes.count(prefix) > 1}
    )
    if duplicate_prefixes:
        duplicates = ", ".join(duplicate_prefixes)
        raise argparse.ArgumentTypeError(
            f"Duplicate geocode prefix(es): {duplicates}."
        )
    return configs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Geocode one or more CSV address columns and map the generated "
            "coordinates to one or more GeoJSON region layers."
        )
    )
    parser.add_argument("--input-file", type=Path, required=True)
    parser.add_argument("--output-file", type=Path, required=True)
    parser.add_argument(
        "--address-column",
        action="append",
        nargs="+",
        required=True,
        help=(
            "Input CSV address column to geocode. Repeat or pass multiple values. "
            "Use COLUMN=prefix to name generated geocode columns."
        ),
    )
    parser.add_argument(
        "--geojson-file",
        action="append",
        nargs="+",
        type=Path,
        required=True,
        help="GeoJSON region layer path. Repeat or pass multiple paths.",
    )
    parser.add_argument(
        "--geocode-prefix",
        default="geocoded",
        help=(
            "Prefix for generated geocoding columns when one address column is "
            "passed without COLUMN=prefix."
        ),
    )
    parser.add_argument(
        "--region-id-col",
        default="id",
        help="GeoJSON property column containing the region ID.",
    )
    parser.add_argument(
        "--region-name-col",
        default="regionName",
        help="GeoJSON property column containing the region name.",
    )
    parser.add_argument(
        "--output-id-col",
        help="Output region ID column. Only valid with one GeoJSON file.",
    )
    parser.add_argument(
        "--output-name-col",
        help="Output region name column. Only valid with one GeoJSON file.",
    )
    parser.add_argument(
        "--output-prefix",
        action="append",
        default=[],
        help=(
            "Output prefix for a GeoJSON layer. Repeat once per GeoJSON file. "
            "Defaults to each GeoJSON filename stem."
        ),
    )

    args = parser.parse_args()
    args.geojson_file = flatten_paths(args.geojson_file)
    try:
        args.address_column = address_configs_from_args(
            flatten_values(args.address_column),
            slugify(args.geocode_prefix),
        )
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))

    if (
        len(args.geojson_file) > 1
        or len(args.address_column) > 1
    ) and (args.output_id_col or args.output_name_col):
        parser.error(
            "--output-id-col and --output-name-col can only be used with one "
            "address column and one GeoJSON file"
        )
    if len(args.output_prefix) > len(args.geojson_file):
        parser.error("pass at most one --output-prefix per --geojson-file")
    return args


def geocode_address(address: object, geolocator: GoogleV3) -> pd.Series:
    empty_result = pd.Series(
        {
            "formatted_address": None,
            "lat": None,
            "long": None,
            "location_type": None,
        }
    )
    if address is None or pd.isna(address):
        return empty_result

    address_text = str(address).strip()
    if not address_text:
        return empty_result

    try:
        location = geolocator.geocode(address_text)
    except Exception as exc:
        print(f"Error geocoding '{address_text}': {type(exc).__name__}: {exc}")
        return empty_result

    if not location:
        return empty_result

    raw = location.raw
    geometry = raw.get("geometry", {}) if isinstance(raw, dict) else {}
    return pd.Series(
        {
            "formatted_address": location.address,
            "lat": location.latitude,
            "long": location.longitude,
            "location_type": geometry.get("location_type"),
        }
    )


def geocode_column(
    df: pd.DataFrame,
    address_column: str,
    prefix: str,
) -> pd.DataFrame:
    if address_column not in df.columns:
        raise ValueError(f"Missing address column: {address_column}")

    load_dotenv_fallback()
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise ValueError("GOOGLE_API_KEY not found. Set it in .env or the environment.")

    geolocator = GoogleV3(api_key=api_key)
    cache: dict[str, pd.Series] = {}
    df = df.copy()

    output_cols = {
        name: f"{prefix}_{name}"
        for name in ["formatted_address", "lat", "long", "location_type"]
    }
    for col in output_cols.values():
        if col not in df.columns:
            df[col] = pd.NA

    needs_geocode = df[output_cols["lat"]].isna() | df[output_cols["long"]].isna()
    rows = df.loc[needs_geocode, address_column].dropna()
    print(f"Geocoding {len(rows):,} rows from '{address_column}'.")

    results = []
    result_indexes = []
    for idx, address in rows.items():
        address_text = str(address).strip()
        if address_text not in cache:
            cache[address_text] = geocode_address(address_text, geolocator)
        results.append(cache[address_text])
        result_indexes.append(idx)

    if results:
        result_df = pd.DataFrame(results, index=result_indexes)
        result_df.columns = [output_cols[col] for col in result_df.columns]
        df.loc[result_df.index, result_df.columns] = result_df

    return df


def load_region_layer(path: Path, id_col: str, name_col: str) -> gpd.GeoDataFrame:
    regions = gpd.read_file(path)
    missing = [col for col in [id_col, name_col, "geometry"] if col not in regions.columns]
    if missing:
        columns = ", ".join(regions.columns)
        raise ValueError(
            f"{path} is missing GeoJSON column(s): {', '.join(missing)}. "
            f"Available columns: {columns}"
        )
    return regions[[id_col, name_col, "geometry"]].copy()


def output_columns(
    path: Path,
    prefix: str | None,
    output_id_col: str | None,
    output_name_col: str | None,
) -> tuple[str, str]:
    if output_id_col or output_name_col:
        return output_id_col or "region_id", output_name_col or "region_name"

    layer_prefix = slugify(prefix or path.stem)
    return f"{layer_prefix}_id", f"{layer_prefix}_name"


def map_to_region(
    df: pd.DataFrame,
    geojson_file: Path,
    lat_col: str,
    lon_col: str,
    region_id_col: str,
    region_name_col: str,
    output_id_col: str,
    output_name_col: str,
) -> pd.DataFrame:
    df = df.copy()
    df[output_id_col] = pd.NA
    df[output_name_col] = pd.NA

    df[lat_col] = pd.to_numeric(df[lat_col], errors="coerce")
    df[lon_col] = pd.to_numeric(df[lon_col], errors="coerce")
    mask = df[lat_col].notna() & df[lon_col].notna()
    print(f"\nMapping {mask.sum():,} geocoded rows to {geojson_file}.")
    if not mask.any():
        return df

    regions = load_region_layer(geojson_file, region_id_col, region_name_col)
    points = gpd.GeoDataFrame(
        df.loc[mask, [lat_col, lon_col]].copy(),
        geometry=gpd.points_from_xy(df.loc[mask, lon_col], df.loc[mask, lat_col]),
        crs="EPSG:4326",
    )
    if regions.crs is not None:
        points = points.to_crs(regions.crs)

    joined = gpd.sjoin(
        points,
        regions,
        how="left",
        predicate="within",
    )
    match_counts = joined.index.value_counts()
    clean_indexes = match_counts[match_counts == 1].index
    clean_matches = joined.loc[joined.index.isin(clean_indexes)].copy()
    clean_matches = clean_matches[clean_matches[region_id_col].notna()]

    df.loc[clean_matches.index, output_id_col] = clean_matches[region_id_col]
    df.loc[clean_matches.index, output_name_col] = clean_matches[region_name_col]

    print(f"Matched rows: {len(clean_matches):,}")
    print(f"Unmatched rows: {joined[region_id_col].isna().sum():,}")
    print(f"Rows with multiple matches skipped: {(match_counts > 1).sum():,}")
    return df


def main() -> None:
    args = parse_args()
    args.output_file.parent.mkdir(parents=True, exist_ok=True)

    geocode_prefix = slugify(args.geocode_prefix)
    lat_col = f"{geocode_prefix}_latitude"
    lon_col = f"{geocode_prefix}_longitude"

    df = pd.read_csv(args.input_file)
    df = geocode_column(df, args.address_column, geocode_prefix)

    used_region_output_cols: set[str] = set()
    for index, geojson_file in enumerate(args.geojson_file):
        prefix = args.output_prefix[index] if index < len(args.output_prefix) else None
        output_id_col, output_name_col = output_columns(
            geojson_file,
            prefix,
            args.output_id_col,
            args.output_name_col,
        )
        duplicate_cols = used_region_output_cols.intersection(
            {output_id_col, output_name_col}
        )
        if duplicate_cols:
            raise ValueError(
                "Duplicate region output column(s): "
                f"{', '.join(sorted(duplicate_cols))}. "
                "Use --output-prefix to give each GeoJSON layer a unique prefix."
            )
        used_region_output_cols.update({output_id_col, output_name_col})

        df = map_to_region(
            df,
            geojson_file,
            lat_col,
            lon_col,
            args.region_id_col,
            args.region_name_col,
            output_id_col,
            output_name_col,
        )

    df.to_csv(args.output_file, index=False)
    print(f"\nSaved {len(df):,} rows to {args.output_file}")


if __name__ == "__main__":
    main()

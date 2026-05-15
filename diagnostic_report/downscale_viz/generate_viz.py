"""
Generate the Dengue Downscale Visualization.

Reads pipeline artifacts and GeoJSONs, generates a single self-contained HTML
file with interactive district/mandal maps and interpretability panels.

Usage:
    uv run python diagnostic_report/downscale_viz/generate_viz.py \
        --district-run-id march-10-run \
        --mandal-run-id   downscale-march-10

Output:
    diagnostic_report/downscale_viz/output/dengue_downscale_viz_<district-run-id>.html
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_DIR = Path(__file__).resolve().parent
TEMPLATE = SCRIPT_DIR / "template.html"
OUTPUT_DIR = SCRIPT_DIR / "output"

# ── Per-model filename fragments — exclude these from "combined" search ────────
_PER_MODEL = ("_nbr_", "_rf_", "_xgb_", "_tse_")


# ── Data loading ──────────────────────────────────────────────────────────────


def find_combined_predictions(results_dir: Path) -> Path:
    """Return the combined (ensemble) predictions CSV, ignoring per-model files."""
    candidates = [
        p
        for p in results_dir.glob("Predictions_*.csv")
        if not any(tag in p.name for tag in _PER_MODEL) and "downscaled" not in p.name
    ]
    if not candidates:
        raise FileNotFoundError(
            f"No combined predictions CSV found in {results_dir}.\n"
            f"Run the main dengue pipeline first (see README.md)."
        )
    if len(candidates) > 1:
        # Prefer the most recently modified file
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        print(f"[warn] multiple combined CSVs found, using: {candidates[0].name}")
    return candidates[0]


def find_downscaled_predictions(results_dir: Path) -> Path:
    candidates = list(results_dir.glob("Predictions_downscaled_*.csv"))
    if not candidates:
        raise FileNotFoundError(
            f"No downscaled predictions CSV found in {results_dir}.\n"
            f"Run the downscale pipeline first (see README.md)."
        )
    if len(candidates) > 1:
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        print(f"[warn] multiple downscaled CSVs found, using: {candidates[0].name}")
    return candidates[0]


def load_parent_data(
    parent_run_dir: Path, parent_level: str
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (predictions_df, thresholds_df)."""
    results_dir = parent_run_dir / "results"
    thresholds_dir = parent_run_dir / "datasets" / "thresholds"

    pred_csv = find_combined_predictions(results_dir)
    print(f"[info] {parent_level} predictions : {pred_csv.relative_to(REPO_ROOT)}")
    preds = pd.read_csv(pred_csv, parse_dates=["startDatePredictedWeek"])

    thresh_csv = thresholds_dir / f"{parent_level}_all_thresholds.csv"
    if not thresh_csv.exists():
        print(
            f"[warn] thresholds CSV not found at {thresh_csv} — zone gauges will be omitted"
        )
        thresholds = pd.DataFrame(
            columns=["region_id", "date", "Mean", "StdDev", "threshold_method"]
        )
    else:
        print(f"[info] thresholds          : {thresh_csv.relative_to(REPO_ROOT)}")
        thresholds = pd.read_csv(thresh_csv, parse_dates=["date"])

    return preds, thresholds


def load_child_data(mandal_run_dir: Path) -> pd.DataFrame:
    results_dir = mandal_run_dir / "results"
    csv = find_downscaled_predictions(results_dir)
    print(f"[info] mandal predictions   : {csv.relative_to(REPO_ROOT)}")
    return pd.read_csv(csv, parse_dates=["startDatePredictedWeek"])


def load_geojsons(geojson_dir: Path, level: str, tolerance: float) -> dict[str, dict]:
    """Load all GeoJSONs for `level` (districts|mandals), return {region_id: feature}."""
    try:
        from shapely.geometry import mapping, shape

        use_shapely = True
    except ImportError:
        print(
            "[warn] shapely not available — GeoJSONs will not be simplified (file may be large)"
        )
        use_shapely = False

    level_dir = geojson_dir / level
    if not level_dir.exists():
        raise FileNotFoundError(
            f"GeoJSON directory not found: {level_dir}\n"
            f"Expected structure: <geojson-dir>/{level}/<region_id>.geojson"
        )

    features: dict[str, dict] = {}
    files = list(level_dir.glob("*.geojson"))
    print(f"[info] loading {len(files)} {level} GeoJSONs ...", end="", flush=True)

    for path in files:
        with open(path) as f:
            gj = json.load(f)

        feat = gj["features"][0] if gj.get("features") else gj
        props = feat.get("properties", {})
        region_id = props.get("region_id") or path.stem

        if use_shapely:
            geom = shape(feat["geometry"])
            geom = geom.simplify(tolerance, preserve_topology=True)
            feat = {
                "type": "Feature",
                "properties": props,
                "geometry": mapping(geom),
            }

        features[region_id] = feat

    print(f" done ({len(features)} regions)")
    return features


# ── Build JS data structure ───────────────────────────────────────────────────


def build_forecast_data(
    dist_preds: pd.DataFrame,
    thresholds: pd.DataFrame,
    mandal_preds: pd.DataFrame,
    dist_geo: dict[str, dict],
    mandal_geo: dict[str, dict],
) -> tuple[list[str], dict]:
    """
    Return (weeks_list, forecast_data_dict) ready for JSON serialisation.

    forecast_data[week][method] = {
        "district": GeoJSON FeatureCollection,
        "mandal":   GeoJSON FeatureCollection,
    }

    District features carry extra properties: mean, std (for threshold gauge).
    """
    # Normalise week column to ISO date string
    dist_preds = dist_preds.copy()
    mandal_preds = mandal_preds.copy()
    dist_preds["_week"] = dist_preds["startDatePredictedWeek"].dt.strftime("%Y-%m-%d")
    mandal_preds["_week"] = mandal_preds["startDatePredictedWeek"].dt.strftime(
        "%Y-%m-%d"
    )

    # Threshold lookup: (region_id, date_str, threshold_method) → (Mean, StdDev)
    thresh_lookup: dict[tuple, tuple] = {}
    if not thresholds.empty:
        thresholds = thresholds.copy()
        thresholds["_date"] = thresholds["date"].dt.strftime("%Y-%m-%d")
        for _, row in thresholds.iterrows():
            key = (row["region_id"], row["_date"], row["threshold_method"])
            thresh_lookup[key] = (row["Mean"], row["StdDev"])

    # District region name lookup from GeoJSONs
    dist_name: dict[str, str] = {
        rid: f["properties"].get("name", rid) for rid, f in dist_geo.items()
    }
    dist_parent: dict[str, str] = {
        rid: f["properties"].get("parent", "") for rid, f in dist_geo.items()
    }
    dist_pname: dict[str, str] = {
        rid: f["properties"].get("parent_name", "") for rid, f in dist_geo.items()
    }

    mandal_name: dict[str, str] = {
        rid: f["properties"].get("name", rid) for rid, f in mandal_geo.items()
    }
    mandal_parent: dict[str, str] = {
        rid: f["properties"].get("parent", "") for rid, f in mandal_geo.items()
    }
    mandal_pname: dict[str, str] = {
        rid: dist_name.get(
            f["properties"].get("parent", ""), f["properties"].get("parent", "")
        )
        for rid, f in mandal_geo.items()
    }

    weeks = sorted(dist_preds["_week"].unique())
    methods = sorted(dist_preds["thresholdMethod"].unique())
    print(f"[info] weeks: {weeks}")
    print(f"[info] methods: {methods}")

    forecast_data: dict = {}

    for week in weeks:
        forecast_data[week] = {}
        dp_week = dist_preds[dist_preds["_week"] == week]
        mp_week = mandal_preds[mandal_preds["_week"] == week]

        for method in methods:
            dp = dp_week[dp_week["thresholdMethod"] == method]
            mp = mp_week[mp_week["thresholdMethod"] == method]

            # Build district FeatureCollection
            dist_features = []
            for _, row in dp.iterrows():
                rid = row["regionID"]
                if rid not in dist_geo:
                    continue
                mean_val, std_val = thresh_lookup.get((rid, week, method), (None, None))
                props = {
                    "id": rid,
                    "name": dist_name.get(rid, rid),
                    "parent": dist_parent.get(rid, ""),
                    "parent_name": dist_pname.get(rid, ""),
                    "prediction": round(float(row["prediction"]), 4),
                    "zone": (
                        float(row["predictionZone"])
                        if pd.notna(row.get("predictionZone"))
                        else 0.0
                    ),
                    "mean": round(float(mean_val), 4) if mean_val is not None else None,
                    "std": round(float(std_val), 4) if std_val is not None else None,
                }
                dist_features.append(
                    {
                        "type": "Feature",
                        "properties": props,
                        "geometry": dist_geo[rid]["geometry"],
                    }
                )

            # Build mandal FeatureCollection
            mandal_features = []
            for _, row in mp.iterrows():
                rid = row["regionID"]
                if rid not in mandal_geo:
                    continue
                props = {
                    "id": rid,
                    "name": mandal_name.get(rid, rid),
                    "parent": mandal_parent.get(rid, ""),
                    "parent_name": mandal_pname.get(rid, ""),
                    "prediction": round(float(row["prediction"]), 4),
                    "zone": (
                        float(row["predictionZone"])
                        if pd.notna(row.get("predictionZone"))
                        else 0.0
                    ),
                }
                mandal_features.append(
                    {
                        "type": "Feature",
                        "properties": props,
                        "geometry": mandal_geo[rid]["geometry"],
                    }
                )

            forecast_data[week][method] = {
                "district": {"type": "FeatureCollection", "features": dist_features},
                "mandal": {"type": "FeatureCollection", "features": mandal_features},
            }

    return weeks, forecast_data


# ── HTML generation ───────────────────────────────────────────────────────────


def generate_html(
    weeks: list[str],
    forecast_data: dict,
    run_id: str,
    parent_level: str = "district",
    child_level: str = "mandal",
) -> str:
    template = TEMPLATE.read_text()

    weeks_js = json.dumps(weeks)
    data_js = json.dumps(forecast_data, separators=(",", ":"))

    inline_js = f"const WEEKS = {weeks_js};\nconst FORECAST_DATA = {data_js};"

    html = template.replace(
        '<script src="forecast_data.js"></script>',
        f"<script>\n{inline_js}\n</script>",
    )
    # Inject run metadata and geography labels
    html = html.replace("{{RUN_ID}}", run_id)
    html = html.replace("{{PARENT_LEVEL}}", parent_level)
    html = html.replace("{{CHILD_LEVEL}}", child_level)
    html = html.replace("{{PARENT_LABEL}}", parent_level.capitalize())
    html = html.replace("{{CHILD_LABEL}}", child_level.capitalize())
    return html


# ── CLI entry point ───────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--parent-run-id",
        default="march-10-run",
        help="Run ID of the parent-level dengue pipeline",
    )
    parser.add_argument(
        "--child-run-id",
        default="downscale-march-10",
        help="Run ID of the downscale pipeline",
    )
    parser.add_argument(
        "--parent-level",
        default="district",
        help="Parent geography (singular, e.g. district, state)",
    )
    parser.add_argument(
        "--child-level",
        default="mandal",
        help="Child geography (singular, e.g. mandal, block)",
    )
    parser.add_argument(
        "--artifacts-dir",
        default="artifacts/ap",
        help="Base artifacts directory (relative to repo root)",
    )
    parser.add_argument(
        "--geojson-dir",
        default="ap_datasets/geojsons/geojsons_AP",
        help="GeoJSON base directory",
    )
    parser.add_argument("--out", default=None, help="Override output HTML path")
    parser.add_argument(
        "--parent-simplify",
        type=float,
        default=0.01,
        help="Shapely simplify tolerance for parent GeoJSONs",
    )
    parser.add_argument(
        "--child-simplify",
        type=float,
        default=0.005,
        help="Shapely simplify tolerance for child GeoJSONs",
    )
    args = parser.parse_args()

    artifacts_dir = REPO_ROOT / args.artifacts_dir
    parent_run_dir = artifacts_dir / args.parent_run_id
    child_run_dir = artifacts_dir / args.child_run_id
    geojson_dir = REPO_ROOT / args.geojson_dir
    parent_level = args.parent_level  # singular, e.g. "district"
    child_level = args.child_level  # singular, e.g. "mandal"

    if not parent_run_dir.exists():
        print(
            f"[error] parent run directory not found: {parent_run_dir}", file=sys.stderr
        )
        print(
            f"        Run the {parent_level}-level dengue pipeline first — see README.md",
            file=sys.stderr,
        )
        sys.exit(1)
    if not child_run_dir.exists():
        print(
            f"[error] child run directory not found: {child_run_dir}", file=sys.stderr
        )
        print(
            "        Run the downscale pipeline first — see README.md", file=sys.stderr
        )
        sys.exit(1)

    print(f"\n[info] parent ({parent_level}) run : {args.parent_run_id}")
    print(f"[info] child  ({child_level}) run  : {args.child_run_id}\n")

    # Load data — GeoJSON dirs use plural convention (districts/, mandals/, blocks/, ...)
    dist_preds, thresholds = load_parent_data(parent_run_dir, parent_level)
    mandal_preds = load_child_data(child_run_dir)
    dist_geo = load_geojsons(geojson_dir, parent_level + "s", args.parent_simplify)
    mandal_geo = load_geojsons(geojson_dir, child_level + "s", args.child_simplify)

    # Build forecast data
    weeks, forecast_data = build_forecast_data(
        dist_preds, thresholds, mandal_preds, dist_geo, mandal_geo
    )

    if not weeks:
        print(
            "[error] no weeks found in predictions data — check CSV contents",
            file=sys.stderr,
        )
        sys.exit(1)

    # Generate HTML
    html = generate_html(
        weeks, forecast_data, args.parent_run_id, parent_level, child_level
    )

    # Write output
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = (
        Path(args.out)
        if args.out
        else OUTPUT_DIR / f"dengue_downscale_viz_{args.parent_run_id}.html"
    )
    out_path.write_text(html, encoding="utf-8")

    size_mb = out_path.stat().st_size / 1_000_000
    print(f"\n[done] {out_path.relative_to(REPO_ROOT)}  ({size_mb:.1f} MB)")
    print("       Open in any browser — fully self-contained, no server needed.")


if __name__ == "__main__":
    main()

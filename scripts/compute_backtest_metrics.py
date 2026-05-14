"""
Backtest Metrics Calculator
===========================
Evaluates forecast accuracy for dengue prediction runs against observed case data.

For each run artifact directory, this script:
  - Loads per-model prediction CSVs (NBR, RF, XGB, Ensemble)
  - Loads the actual observed case data from the same run's datasets/
  - Joins predictions to actuals on (regionID, week)
  - Computes MAE, RMSE, and zone accuracy for every combination of
    (model × threshold method)
  - Optionally breaks metrics down per-week and per-district

Output:
  - metrics_summary.csv   — one row per (run, model, threshold_method)
  - metrics_per_week.csv  — one row per (run, model, threshold_method, week)
  - metrics_per_district.csv — one row per (run, model, threshold_method, district)
  - metrics.json          — same data as JSON (for the interactive report)

Usage:
    python scripts/compute_backtest_metrics.py

    # Or point at specific runs:
    python scripts/compute_backtest_metrics.py --runs artifacts/ap/backtest-20260306 artifacts/ap/run-2026-03-13

    # Change output directory:
    python scripts/compute_backtest_metrics.py --output /tmp/my_metrics

Tuning:
    Edit the CONFIGURATION section below to change which runs, models, or
    threshold methods to evaluate, or to add new metrics.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import warnings
from pathlib import Path

import pandas as pd

warnings.filterwarnings("ignore", category=FutureWarning)


# ---------------------------------------------------------------------------
# CONFIGURATION — edit these to change what gets evaluated
# ---------------------------------------------------------------------------

# Root directory containing run artifact subdirectories
ARTIFACTS_ROOT = Path(__file__).parent.parent / "artifacts" / "ap"

# Default runs to evaluate (relative to ARTIFACTS_ROOT).
# Set to None to auto-discover all subdirs that look like production runs.
DEFAULT_RUNS = [
    "backtest-20260306",
    "run-2026-03-13",
    "run-2026-03-20",
    "run-2026-03-27",
    "run-2026-04-03",
    "clean-config-test",
]

# Model short names → patterns that appear in prediction CSV filenames
MODEL_FILE_PATTERNS = {
    "nbr": r"District_nbr_",
    "rf": r"District_rf_",
    "xgb": r"District_xgb_",
    # Ensemble: the file with no model suffix but "District" in the name
    "ensemble": r"District_\d{8}\.csv$",
}

# Threshold method names as they appear in the 'thresholdMethod' column
THRESHOLD_METHODS = ["historical", "previousNweeks"]

# Default output directory
DEFAULT_OUTPUT = Path(__file__).parent.parent / "metrics_output"


# ---------------------------------------------------------------------------
# WHO zone classification helper
# ---------------------------------------------------------------------------


def classify_who_zone(value: float, t0: float, t1: float, t2: float) -> int | None:
    """
    Classify a case count into WHO risk zones (1–4) using threshold bands.

    Zones:
        1 = Low     (value <= T0 = Mean)
        2 = Medium  (T0 < value <= T1 = Mean + σ)
        3 = High    (T1 < value <= T2 = Mean + 2σ)
        4 = Critical (value > T2)

    Returns None if any threshold is NaN or infinite (insufficient data).

    Note: Old runs (pre PRISM-H fix) used T2/T3 naming for what is now T1/T2.
    This function always receives normalized values regardless of source naming.
    """
    if any(math.isnan(v) or math.isinf(v) for v in [t0, t1, t2]):
        return None
    if value <= t0:
        return 1
    if value <= t1:
        return 2
    if value <= t2:
        return 3
    return 4


# ---------------------------------------------------------------------------
# Run artifact discovery and loading
# ---------------------------------------------------------------------------


def find_prediction_csv(results_dir: Path, model_key: str) -> Path | None:
    """Return the single CSV in results_dir that matches model_key's pattern."""
    pattern = MODEL_FILE_PATTERNS[model_key]
    candidates = [f for f in results_dir.glob("*.csv") if re.search(pattern, f.name)]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        # Prefer the most recent (highest date suffix)
        return sorted(candidates)[-1]
    return None


def find_best_cases_csv() -> Path | None:
    """
    Return the cases CSV with the most recent data across all run artifact directories.

    Each run's cases_district_sampled.csv contains actuals only up to that run's
    cut-off date. To evaluate predictions (which are for weeks *after* the run date),
    we need a cases file from a later run that contains those weeks as actuals.

    Strategy: pick the cases CSV whose last date is the latest.
    """
    best_path = None
    best_date = pd.Timestamp.min

    for csv_path in ARTIFACTS_ROOT.glob("*/datasets/cases_district_sampled.csv"):
        try:
            df = pd.read_csv(
                csv_path,
                usecols=["metadata.primaryDate"],
                parse_dates=["metadata.primaryDate"],
            )
            last = df["metadata.primaryDate"].max()
            if last > best_date:
                best_date = last
                best_path = csv_path
        except Exception:
            continue

    if best_path:
        print(
            f"  [actuals] Using {best_path.relative_to(ARTIFACTS_ROOT)} "
            f"(data through {best_date.date()})"
        )
    return best_path


def load_actuals(cases_csv: Path) -> pd.DataFrame:
    """
    Load observed case data and return a DataFrame with columns:
        regionID, week_start (datetime), actual_cases
    """
    df = pd.read_csv(cases_csv, parse_dates=["metadata.primaryDate"])
    df = df.rename(
        columns={
            "location.admin2.ID": "regionID",
            "metadata.primaryDate": "week_start",
            "case": "actual_cases",
        }
    )
    return df[["regionID", "week_start", "actual_cases"]]


def normalise_threshold_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Old runs (pre PRISM-H fix) named thresholds T2.00/T3.00;
    new runs use T1.00/T2.00.  Rename to t0, t1, t2 for uniform access.
    """
    if "T3.00" in df.columns:
        # Old naming: T0=Mean, T2=Mean+σ, T3=Mean+2σ
        df = df.rename(columns={"T0.00": "t0", "T2.00": "t1", "T3.00": "t2"})
    elif "T2.00" in df.columns:
        # New naming: T0=Mean, T1=Mean+σ, T2=Mean+2σ
        df = df.rename(columns={"T0.00": "t0", "T1.00": "t1", "T2.00": "t2"})
    else:
        # No threshold columns (e.g. bare ensemble without threshold rows)
        df["t0"] = float("nan")
        df["t1"] = float("nan")
        df["t2"] = float("nan")
    return df


def load_predictions(results_dir: Path, model_key: str) -> pd.DataFrame | None:
    """
    Load predictions for one model and normalise columns.
    Returns None if no matching file is found.
    """
    csv_path = find_prediction_csv(results_dir, model_key)
    if csv_path is None:
        return None

    df = pd.read_csv(csv_path, parse_dates=["startDatePredictedWeek"])
    df = df.rename(columns={"startDatePredictedWeek": "week_start"})
    df = normalise_threshold_columns(df)

    # Keep only the threshold methods we care about
    if "thresholdMethod" in df.columns:
        df = df[df["thresholdMethod"].isin(THRESHOLD_METHODS)]

    return df


# ---------------------------------------------------------------------------
# Metric computation
# ---------------------------------------------------------------------------


def compute_metrics_for_group(group: pd.DataFrame) -> dict:
    """
    Compute MAE, RMSE, zone accuracy for a group of (prediction, actual) pairs.

    group must have columns: prediction, actual_cases, predicted_zone, actual_zone
    """
    n = len(group)
    if n == 0:
        return {"n": 0, "mae": None, "rmse": None, "zone_accuracy": None}

    errors = group["prediction"] - group["actual_cases"]
    mae = errors.abs().mean()
    rmse = math.sqrt((errors**2).mean())

    # Zone accuracy: only where both zones are available
    zone_pairs = group.dropna(subset=["predicted_zone", "actual_zone"])
    zone_acc = (
        (zone_pairs["predicted_zone"] == zone_pairs["actual_zone"]).mean()
        if len(zone_pairs) > 0
        else None
    )

    return {
        "n": n,
        "mae": round(mae, 4),
        "rmse": round(rmse, 4),
        "zone_accuracy": round(zone_acc, 4) if zone_acc is not None else None,
    }


def evaluate_run(run_dir: Path) -> list[dict]:
    """
    Evaluate all models × all threshold methods for a single run directory.
    Returns a list of result dicts (one per model × threshold method combination).
    """
    run_id = run_dir.name
    results_dir = run_dir / "results"
    if not results_dir.exists():
        print(f"  [skip] No results/ in {run_id}")
        return []

    cases_csv = find_best_cases_csv()
    if cases_csv is None:
        print(f"  [skip] No cases CSV for {run_id}")
        return []

    actuals = load_actuals(cases_csv)
    records = []

    for model_key in MODEL_FILE_PATTERNS:
        preds = load_predictions(results_dir, model_key)
        if preds is None:
            print(f"  [skip] No {model_key} predictions in {run_id}")
            continue

        # Join predictions to actuals
        merged = preds.merge(
            actuals,
            on=["regionID", "week_start"],
            how="inner",
        )

        if merged.empty:
            print(
                f"  [warn] No matching weeks for {run_id}/{model_key} — "
                "predictions may be entirely in the future"
            )
            continue

        # Classify actual cases into WHO zones using the predicted threshold bands
        merged["actual_zone"] = merged.apply(
            lambda r: classify_who_zone(r["actual_cases"], r["t0"], r["t1"], r["t2"]),
            axis=1,
        )
        merged["predicted_zone"] = merged.get("whoZone", merged.get("predictionZone"))

        # Group by threshold method
        for method, method_group in merged.groupby("thresholdMethod"):
            metrics = compute_metrics_for_group(method_group)

            # Per-week breakdown
            per_week = {}
            for week, week_group in method_group.groupby("week_start"):
                per_week[str(week.date())] = compute_metrics_for_group(week_group)

            # Per-district breakdown
            per_district = {}
            for district, dist_group in method_group.groupby("regionID"):
                per_district[district] = compute_metrics_for_group(dist_group)

            records.append(
                {
                    "run_id": run_id,
                    "model": model_key,
                    "threshold_method": method,
                    **metrics,
                    "per_week": per_week,
                    "per_district": per_district,
                }
            )

        print(f"  [ok] {run_id} / {model_key} ({len(merged)} matched rows)")

    return records


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def flatten_for_csv(
    records: list[dict], breakdown_key: str | None = None
) -> pd.DataFrame:
    """
    Flatten the nested records list into a DataFrame suitable for CSV output.
    breakdown_key: 'per_week' or 'per_district' for breakdown tables, None for summary.
    """
    rows = []
    for rec in records:
        base = {k: v for k, v in rec.items() if k not in ("per_week", "per_district")}
        if breakdown_key is None:
            rows.append(base)
        else:
            for key, m in rec.get(breakdown_key, {}).items():
                rows.append({**base, breakdown_key.replace("per_", ""): key, **m})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Compute backtest metrics for dengue forecast runs."
    )
    parser.add_argument(
        "--runs",
        nargs="+",
        metavar="RUN_DIR",
        help="Run artifact directories to evaluate (relative to artifacts/ap/ or absolute paths)",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        metavar="DIR",
        help="Directory to write output files (default: metrics_output/)",
    )
    args = parser.parse_args()

    # Resolve run directories
    if args.runs:
        run_dirs = []
        for r in args.runs:
            p = Path(r)
            # If absolute or the path exists as-is (e.g. "artifacts/ap/run-xxx" from project root),
            # use it directly. Otherwise treat as a bare name like "run-2026-03-13".
            if p.is_absolute() or p.exists():
                run_dirs.append(p)
            else:
                run_dirs.append(ARTIFACTS_ROOT / r)
    else:
        run_dirs = [ARTIFACTS_ROOT / r for r in DEFAULT_RUNS]

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    all_records = []
    for run_dir in run_dirs:
        if not run_dir.exists():
            print(f"[warn] Run directory not found: {run_dir}")
            continue
        print(f"\nEvaluating: {run_dir.name}")
        all_records.extend(evaluate_run(run_dir))

    if not all_records:
        print(
            "\nNo metrics computed — check that runs have results/ and datasets/ directories."
        )
        return

    # Write CSV outputs
    summary_df = flatten_for_csv(all_records)
    summary_df.drop(columns=["per_week", "per_district"], errors="ignore").to_csv(
        output_dir / "metrics_summary.csv", index=False
    )

    week_df = flatten_for_csv(all_records, breakdown_key="per_week")
    week_df.to_csv(output_dir / "metrics_per_week.csv", index=False)

    district_df = flatten_for_csv(all_records, breakdown_key="per_district")
    district_df.to_csv(output_dir / "metrics_per_district.csv", index=False)

    # Write JSON (for the interactive report)
    # Structure: {run_id: {model: {threshold_method: {mae, rmse, zone_accuracy, per_week, per_district}}}}
    json_out: dict = {}
    for rec in all_records:
        run = rec["run_id"]
        model = rec["model"]
        method = rec["threshold_method"]
        json_out.setdefault(run, {}).setdefault(model, {})[method] = {
            "n": rec["n"],
            "mae": rec["mae"],
            "rmse": rec["rmse"],
            "zone_accuracy": rec["zone_accuracy"],
            "per_week": rec["per_week"],
            "per_district": rec["per_district"],
        }

    with open(output_dir / "metrics.json", "w") as f:
        json.dump(json_out, f, indent=2)

    print(f"\nOutputs written to {output_dir}/")
    print(f"  metrics_summary.csv     ({len(summary_df)} rows)")
    print(f"  metrics_per_week.csv    ({len(week_df)} rows)")
    print(f"  metrics_per_district.csv({len(district_df)} rows)")
    print("  metrics.json")

    # Print a quick human-readable summary
    print("\n--- Summary (MAE / RMSE / ZoneAcc) ---")
    for run_id, models in json_out.items():
        print(f"\n{run_id}")
        for model, methods in models.items():
            for method, m in methods.items():
                zone_str = (
                    f"{m['zone_accuracy']*100:.1f}%" if m["zone_accuracy"] else "N/A"
                )
                print(
                    f"  {model:10s} / {method:15s}  "
                    f"MAE={m['mae']:.3f}  RMSE={m['rmse']:.3f}  Zone={zone_str}  (n={m['n']})"
                )


if __name__ == "__main__":
    main()

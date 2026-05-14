"""
Backtest Metrics Calculator
===========================
Compares dengue forecast predictions against observed case data and produces
a visual HTML report + CSV/JSON exports.

QUICK START
-----------
Run from the project root with no arguments to evaluate all known runs:

    uv run python scripts/compute_backtest_metrics.py

This writes to metrics_output/ by default:
    metrics_report.html     ← open this in a browser
    metrics_summary.csv
    metrics_per_week.csv
    metrics_per_district.csv
    metrics.json

EVALUATING SPECIFIC RUNS
-------------------------
Pass one or more run directories (name, relative path, or absolute path):

    uv run python scripts/compute_backtest_metrics.py --runs backtest-20260306 run-2026-03-13

    uv run python scripts/compute_backtest_metrics.py --runs artifacts/ap/run-2026-04-03

    uv run python scripts/compute_backtest_metrics.py --output /tmp/my_eval --runs backtest-20260306

HOW ACTUALS ARE FOUND
---------------------
Predictions in a run cover weeks *after* the run date. The script automatically
finds the most recent cases CSV across all run artifacts to use as ground truth.
If you have newer case data, add it under any run's datasets/ folder — it will
be picked up automatically.

ADDING NEW METRICS
------------------
Edit compute_metrics_for_group() below. The function receives a DataFrame with
columns: prediction, actual_cases, predicted_zone, actual_zone — add whatever
you need and include it in the returned dict.
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
# CONFIGURATION
# ---------------------------------------------------------------------------

ARTIFACTS_ROOT = Path(__file__).parent.parent / "artifacts" / "ap"
PREPARED_CASES_CSV = (
    Path(__file__).parent.parent / "prepared_data" / "district" / "cases_daily.csv"
)


MODEL_FILE_PATTERNS = {
    "nbr": r"District_nbr_",
    "rf": r"District_rf_",
    "xgb": r"District_xgb_",
    "ensemble": r"District_\d{8}\.csv$",
}

MODEL_LABELS = {
    "nbr": "Negative Binomial",
    "rf": "Random Forest",
    "xgb": "XGBoost",
    "ensemble": "Ensemble",
}

THRESHOLD_METHODS = ["historical", "previousNweeks"]

DEFAULT_OUTPUT = Path(__file__).parent.parent / "metrics_output"


# ---------------------------------------------------------------------------
# WHO zone classification
# ---------------------------------------------------------------------------


def classify_who_zone(value: float, t0: float, t1: float, t2: float) -> int | None:
    """
    Classify a case count into WHO risk zones 1–4 using threshold bands.

        Zone 1 – Low       value <= T0 (= Mean)
        Zone 2 – Medium    T0 < value <= T1 (= Mean + σ)
        Zone 3 – High      T1 < value <= T2 (= Mean + 2σ)
        Zone 4 – Critical  value > T2

    Returns None if thresholds are NaN/inf (insufficient historical data).
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
# Data loading helpers
# ---------------------------------------------------------------------------


def find_prediction_csv(results_dir: Path, model_key: str) -> Path | None:
    pattern = MODEL_FILE_PATTERNS[model_key]
    candidates = [f for f in results_dir.glob("*.csv") if re.search(pattern, f.name)]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        return sorted(candidates)[-1]
    return None


def find_best_cases_csv() -> tuple[Path | None, str, str]:
    """Return (path, date_str, source_kind) for the most up-to-date cases data.

    Checks prepared_data/district/cases_daily.csv first (always up to date),
    then falls back to the most recent cases_district_sampled.csv across artifact runs.
    source_kind is 'daily' or 'sampled' — used by load_actuals to parse correctly.
    """
    # --- preferred: prepared_data daily CSV (real data, always fresh) ---
    if PREPARED_CASES_CSV.exists():
        try:
            df = pd.read_csv(PREPARED_CASES_CSV, usecols=["date"], parse_dates=["date"])
            last = df["date"].max()
            return PREPARED_CASES_CSV, str(last.date()), "daily"
        except Exception:
            pass

    # --- fallback: most recent sampled CSV inside artifact runs ---
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

    date_str = str(best_date.date()) if best_path else "unknown"
    return best_path, date_str, "sampled"


def load_actuals(cases_csv: Path, source_kind: str) -> pd.DataFrame:
    if source_kind == "daily":
        df = pd.read_csv(cases_csv, parse_dates=["date"])
        df = df.rename(
            columns={
                "region_id": "regionID",
                "date": "date",
                "case_count": "actual_cases",
            }
        )
        # aggregate daily → weekly using Friday as the week label (matches pipeline sampling)
        df["week_start"] = df["date"] + pd.to_timedelta(
            (4 - df["date"].dt.dayofweek) % 7, unit="D"
        )
        return df.groupby(["regionID", "week_start"], as_index=False)[
            "actual_cases"
        ].sum()[["regionID", "week_start", "actual_cases"]]
    else:
        df = pd.read_csv(cases_csv, parse_dates=["metadata.primaryDate"])
        return df.rename(
            columns={
                "location.admin2.ID": "regionID",
                "metadata.primaryDate": "week_start",
                "case": "actual_cases",
            }
        )[["regionID", "week_start", "actual_cases"]]


def normalise_threshold_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename T0/T2/T3 (old) or T0/T1/T2 (new) to uniform t0, t1, t2."""
    if "T3.00" in df.columns:
        return df.rename(columns={"T0.00": "t0", "T2.00": "t1", "T3.00": "t2"})
    if "T2.00" in df.columns:
        return df.rename(columns={"T0.00": "t0", "T1.00": "t1", "T2.00": "t2"})
    df["t0"] = float("nan")
    df["t1"] = float("nan")
    df["t2"] = float("nan")
    return df


def load_predictions(results_dir: Path, model_key: str) -> pd.DataFrame | None:
    csv_path = find_prediction_csv(results_dir, model_key)
    if csv_path is None:
        return None
    df = pd.read_csv(csv_path, parse_dates=["startDatePredictedWeek"])
    df = df.rename(columns={"startDatePredictedWeek": "week_start"})
    df = normalise_threshold_columns(df)
    if "thresholdMethod" in df.columns:
        df = df[df["thresholdMethod"].isin(THRESHOLD_METHODS)]
    return df


# ---------------------------------------------------------------------------
# Metric computation
# ---------------------------------------------------------------------------


class Metrics:
    """
    Each static method receives predicted and actual series and returns a single
    rounded float, or None when the metric can't be computed.

    To add a new metric:
      1. Add a static method here with signature (predicted, actual) -> float | None.
      2. Include it in compute_metrics_for_group() below.
      3. Add it to the HTML dropdown, district table columns, and CLI summary as needed.
    """

    @staticmethod
    def mae(predicted: pd.Series, actual: pd.Series) -> float:
        """Mean Absolute Error — average absolute difference (same units as case count)."""
        return round((predicted - actual).abs().mean(), 4)

    @staticmethod
    def rmse(predicted: pd.Series, actual: pd.Series) -> float:
        """Root Mean Squared Error — penalises large errors more than MAE."""
        return round(math.sqrt(((predicted - actual) ** 2).mean()), 4)

    @staticmethod
    def nrmse(predicted: pd.Series, actual: pd.Series) -> float | None:
        """Normalised RMSE = RMSE / mean(actual). Dimensionless; comparable across districts."""
        mean_actual = actual.mean()
        if mean_actual <= 0:
            return None
        return round(Metrics.rmse(predicted, actual) / mean_actual, 4)

    @staticmethod
    def bias(predicted: pd.Series, actual: pd.Series) -> float:
        """Signed mean error (prediction − actual). Positive = over-predicting."""
        return round((predicted - actual).mean(), 4)

    @staticmethod
    def zone_accuracy(
        predicted_zone: pd.Series, actual_zone: pd.Series
    ) -> float | None:
        """Fraction of district-weeks where predicted WHO zone matches actual zone."""
        pairs = pd.concat([predicted_zone, actual_zone], axis=1).dropna()
        if pairs.empty:
            return None
        return round((pairs.iloc[:, 0] == pairs.iloc[:, 1]).mean(), 4)


def compute_metrics_for_group(group: pd.DataFrame) -> dict:
    """
    Compute all metrics for a (prediction, actual) group.
    Input columns: prediction, actual_cases, predicted_zone, actual_zone.
    """
    n = len(group)
    if n == 0:
        return {
            "n": 0,
            "mae": None,
            "rmse": None,
            "nrmse": None,
            "zone_accuracy": None,
            "bias": None,
        }

    predicted = group["prediction"]
    actual = group["actual_cases"]

    return {
        "n": n,
        "mae": Metrics.mae(predicted, actual),
        "rmse": Metrics.rmse(predicted, actual),
        "nrmse": Metrics.nrmse(predicted, actual),
        "zone_accuracy": Metrics.zone_accuracy(
            group["predicted_zone"], group["actual_zone"]
        ),
        "bias": Metrics.bias(predicted, actual),
    }


def evaluate_run(run_dir: Path, actuals: pd.DataFrame) -> list[dict]:
    run_id = run_dir.name
    results_dir = run_dir / "results"
    if not results_dir.exists():
        print(
            f"  skip  {run_id}  — no results/ directory (did the pipeline run complete successfully?)"
        )
        return []

    records = []
    for model_key in MODEL_FILE_PATTERNS:
        preds = load_predictions(results_dir, model_key)
        if preds is None:
            continue

        # Join on ISO year+week so sampling-day differences (Fri vs Sun etc.) don't break the match
        preds["_iso_year"] = preds["week_start"].dt.isocalendar().year
        preds["_iso_week"] = preds["week_start"].dt.isocalendar().week
        actuals["_iso_year"] = actuals["week_start"].dt.isocalendar().year
        actuals["_iso_week"] = actuals["week_start"].dt.isocalendar().week
        merged = preds.merge(
            actuals.drop(columns=["week_start"]),
            on=["regionID", "_iso_year", "_iso_week"],
            how="inner",
        ).drop(columns=["_iso_year", "_iso_week"])
        preds.drop(columns=["_iso_year", "_iso_week"], inplace=True)
        actuals.drop(columns=["_iso_year", "_iso_week"], inplace=True)
        if merged.empty:
            print(
                f"  skip  {run_id}/{model_key}  — predictions don't overlap with actuals\n"
                f"         (actuals end {actuals['week_start'].max().date()}, "
                f"predictions start {preds['week_start'].min().date()})\n"
                f"         Use a run_date at least 5–6 weeks before {actuals['week_start'].max().date()}"
            )
            continue

        merged["actual_zone"] = merged.apply(
            lambda r: classify_who_zone(r["actual_cases"], r["t0"], r["t1"], r["t2"]),
            axis=1,
        )
        merged["predicted_zone"] = merged.get("whoZone", merged.get("predictionZone"))

        for method, method_group in merged.groupby("thresholdMethod"):
            m = compute_metrics_for_group(method_group)

            per_week = {
                str(w.date()): compute_metrics_for_group(wg)
                for w, wg in method_group.groupby("week_start")
            }
            per_district = {
                d: compute_metrics_for_group(dg)
                for d, dg in method_group.groupby("regionID")
            }

            records.append(
                {
                    "run_id": run_id,
                    "model": model_key,
                    "threshold_method": method,
                    **m,
                    "per_week": per_week,
                    "per_district": per_district,
                }
            )

        weeks = sorted(merged["week_start"].dt.date.unique())
        print(
            f"  ok    {run_id}/{model_key}  "
            f"({len(merged)} rows, weeks {weeks[0]} → {weeks[-1]})"
        )

    return records


# ---------------------------------------------------------------------------
# CSV / JSON output
# ---------------------------------------------------------------------------


def flatten_for_csv(
    records: list[dict], breakdown_key: str | None = None
) -> pd.DataFrame:
    rows = []
    for rec in records:
        base = {k: v for k, v in rec.items() if k not in ("per_week", "per_district")}
        if breakdown_key is None:
            rows.append(base)
        else:
            col = breakdown_key.replace("per_", "")
            for key, m in rec.get(breakdown_key, {}).items():
                rows.append({**base, col: key, **m})
    return pd.DataFrame(rows)


def build_json(records: list[dict]) -> dict:
    out: dict = {}
    for rec in records:
        out.setdefault(rec["run_id"], {}).setdefault(rec["model"], {})[
            rec["threshold_method"]
        ] = {
            "n": rec["n"],
            "mae": rec["mae"],
            "rmse": rec["rmse"],
            "nrmse": rec["nrmse"],
            "zone_accuracy": rec["zone_accuracy"],
            "bias": rec["bias"],
            "per_week": rec["per_week"],
            "per_district": rec["per_district"],
        }
    return out


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------


def _pct(v: float | None) -> str:
    return f"{v*100:.1f}%" if v is not None else "—"


def _fmt(v: float | None, decimals: int = 3) -> str:
    return f"{v:.{decimals}f}" if v is not None else "—"


def _mae_color(mae: float | None, lo: float, hi: float) -> str:
    """Green (low MAE) → yellow → red (high MAE)."""
    if mae is None:
        return "#f5f5f5"
    t = max(0.0, min(1.0, (mae - lo) / (hi - lo + 1e-9)))
    r = int(255 * t + 60 * (1 - t))
    g = int(200 * (1 - t) + 180 * t)
    b = int(60 * (1 - t) + 60 * t)
    return f"rgb({r},{g},{b})"


def _zone_color(acc: float | None) -> str:
    """Green (high accuracy) → red (low)."""
    if acc is None:
        return "#f5f5f5"
    r = int(220 * (1 - acc) + 60 * acc)
    g = int(60 * (1 - acc) + 180 * acc)
    b = 60
    return f"rgb({r},{g},{b})"


def build_html_report(json_data: dict, actuals_date: str) -> str:
    runs = list(json_data.keys())
    models = list(MODEL_FILE_PATTERNS.keys())

    all_mae = [
        json_data[r][m][th]["mae"]
        for r in runs
        for m in models
        for th in THRESHOLD_METHODS
        if r in json_data
        and m in json_data[r]
        and th in json_data[r][m]
        and json_data[r][m][th]["mae"] is not None
    ]
    mae_lo, mae_hi = (min(all_mae), max(all_mae)) if all_mae else (0, 5)

    data_json = json.dumps(json_data)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dengue Forecast — Backtest Metrics</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #f7f8fa; color: #222; font-size: 14px; }}
  header {{ background: #1a2e44; color: #fff; padding: 18px 32px; }}
  header h1 {{ font-size: 20px; font-weight: 600; }}
  header p  {{ font-size: 12px; opacity: .7; margin-top: 4px; }}
  .run-checkboxes {{ display: flex; flex-wrap: wrap; gap: 6px; }}
  .run-checkboxes label {{ display: flex; align-items: center; gap: 4px; font-weight: 400;
    color: #333; cursor: pointer; background: #f0f2f5; border: 1px solid #d0d4db;
    border-radius: 4px; padding: 3px 8px; font-size: 12px; }}
  .run-checkboxes label:has(input:checked) {{ background: #dbeafe; border-color: #3b82f6; color: #1d4ed8; }}
  .controls {{ background: #fff; border-bottom: 1px solid #e0e3e8;
               padding: 12px 32px; display: flex; gap: 24px; align-items: center; flex-wrap: wrap; }}
  .controls label {{ font-size: 12px; font-weight: 600; color: #555; margin-right: 6px; }}
  .controls select, .controls input[type=radio] {{ cursor: pointer; }}
  .controls .group {{ display: flex; align-items: center; gap: 8px; }}
  .radio-group {{ display: flex; gap: 10px; }}
  .radio-group label {{ font-weight: 400; color: #333; cursor: pointer; }}
  main {{ padding: 24px 32px; max-width: 1400px; margin: 0 auto; }}
  section {{ background: #fff; border-radius: 8px; padding: 20px 24px;
             margin-bottom: 20px; box-shadow: 0 1px 4px rgba(0,0,0,.06); }}
  section h2 {{ font-size: 15px; font-weight: 600; color: #1a2e44; margin-bottom: 4px; }}
  section p.sub {{ font-size: 12px; color: #888; margin-bottom: 16px; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
  th {{ background: #f0f2f5; text-align: center; padding: 8px 12px;
        font-weight: 600; border: 1px solid #dde1e7; white-space: nowrap; }}
  th.run-col {{ text-align: left; min-width: 160px; }}
  td {{ padding: 7px 12px; border: 1px solid #dde1e7; text-align: center; vertical-align: middle; }}
  td.run-label {{ text-align: left; font-family: monospace; font-size: 12px; }}
  td.cell {{ font-size: 13px; font-weight: 600; }}
  td.na {{ color: #aaa; font-weight: 400; font-style: italic; }}
  .metric-line {{ font-size: 11px; font-weight: 400; color: #555; }}
  .chart-row {{ display: flex; gap: 16px; flex-wrap: wrap; }}
  .chart-box {{ flex: 1; min-width: 280px; }}
  .chart-box h3 {{ font-size: 13px; font-weight: 600; margin-bottom: 8px; color: #444; }}
  .district-table {{ max-height: 420px; overflow-y: auto; }}
  .district-table table {{ font-size: 12px; }}
  .district-table td, .district-table th {{ padding: 5px 10px; }}
  .sortable {{ cursor: pointer; user-select: none; }}
  .sortable:hover {{ background: #e3e7ee; }}
  .tag {{ display: inline-block; padding: 2px 8px; border-radius: 10px;
          font-size: 11px; font-weight: 600; }}
  .tag-hist {{ background: #e8f0fe; color: #1a56db; }}
  .tag-prev {{ background: #fef3c7; color: #92400e; }}
  .no-data {{ text-align: center; padding: 40px; color: #aaa; font-style: italic; }}
  .legend-row {{ display: flex; gap: 16px; margin-bottom: 12px; flex-wrap: wrap; }}
  .legend-item {{ display: flex; align-items: center; gap: 6px; font-size: 12px; }}
  .legend-swatch {{ width: 14px; height: 14px; border-radius: 3px; }}
</style>
</head>
<body>

<header>
  <h1>Dengue Forecast — Backtest Accuracy Report</h1>
  <p>Actuals through {actuals_date} &nbsp;·&nbsp; {len(runs)} run(s) evaluated
     &nbsp;·&nbsp; Models: NBR, RF, XGB, Ensemble &nbsp;·&nbsp; Threshold: historical &amp; previousNweeks</p>
</header>

<div class="controls">
  <div class="group">
    <label>Threshold method</label>
    <div class="radio-group" id="method-radios">
      <label><input type="radio" name="method" value="historical" checked> Historical</label>
      <label><input type="radio" name="method" value="previousNweeks"> Previous N-weeks</label>
    </div>
  </div>
  <div class="group">
    <label>Primary metric</label>
    <select id="metric-select">
      <option value="mae">MAE (lower is better)</option>
      <option value="rmse">RMSE (lower is better)</option>
      <option value="nrmse">NRMSE — RMSE ÷ mean(actual) (lower is better)</option>
      <option value="zone_accuracy">Zone Accuracy (higher is better)</option>
      <option value="bias">Bias (closer to 0 is better)</option>
    </select>
  </div>
  <div class="group">
    <label>Runs</label>
    <div class="run-checkboxes" id="run-checkboxes"></div>
  </div>
</div>

<main>

<!-- ── Section 1: Heatmap ─────────────────────────────────────────────── -->
<section>
  <h2>Model Performance Heatmap</h2>
  <p class="sub">Each cell shows the selected metric across all matched weeks.
     Color: green = good, red = poor.</p>
  <div id="heatmap-container"></div>
</section>

<!-- ── Section 2: Error over forecast horizon ────────────────────────── -->
<section>
  <h2>Error over forecast horizon</h2>
  <p class="sub">How error changes week-by-week into the future. Each run shows 4–8 forecast weeks.
     Longer-horizon weeks typically have higher error.</p>
  <div class="chart-row" id="horizon-charts"></div>
</section>

<!-- ── Section 3: District breakdown ─────────────────────────────────── -->
<section>
  <h2>District-level breakdown</h2>
  <p class="sub">Click column headers to sort.</p>
  <div style="display:flex;gap:12px;align-items:center;margin-bottom:10px;flex-wrap:wrap;">
    <div><label style="font-size:12px;font-weight:600;color:#555;margin-right:6px;">Run</label>
      <select id="run-select"></select></div>
    <div><label style="font-size:12px;font-weight:600;color:#555;margin-right:6px;">Model</label>
      <select id="district-model-select">
        <option value="nbr">Negative Binomial (NBR)</option>
        <option value="rf">Random Forest</option>
        <option value="xgb">XGBoost</option>
        <option value="ensemble">Ensemble</option>
      </select></div>
  </div>
  <div class="district-table" id="district-table-wrap"></div>
</section>

</main>

<script>
const DATA = {data_json};

const MODELS = ['nbr','rf','xgb','ensemble'];
const MODEL_LABELS = {{
  nbr: 'Negative Binomial',
  rf: 'Random Forest',
  xgb: 'XGBoost',
  ensemble: 'Ensemble'
}};
const RUNS = Object.keys(DATA);
const MAE_LO = {mae_lo:.3f}, MAE_HI = {mae_hi:.3f};

let charts = {{}};

function getMethod() {{
  return document.querySelector('input[name=method]:checked').value;
}}
function getMetric() {{
  return document.getElementById('metric-select').value;
}}
function getSelectedRuns() {{
  return Array.from(document.querySelectorAll('#run-checkboxes input:checked')).map(el => el.value);
}}
function getDistrictRun() {{
  return document.getElementById('run-select').value;
}}
function getDistrictModel() {{
  return document.getElementById('district-model-select').value;
}}

function fmt(v, metric) {{
  if (v == null) return '—';
  if (metric === 'zone_accuracy') return (v*100).toFixed(1) + '%';
  if (metric === 'nrmse') return v.toFixed(3);
  return v.toFixed(3);
}}

function metricLabel(metric) {{
  return {{
    mae: 'MAE', rmse: 'RMSE', nrmse: 'NRMSE', zone_accuracy: 'Zone Accuracy', bias: 'Bias'
  }}[metric] || metric;
}}

function maeColor(v) {{
  if (v == null) return '#f5f5f5';
  const t = Math.max(0, Math.min(1, (v - MAE_LO) / (MAE_HI - MAE_LO + 1e-9)));
  const r = Math.round(255*t + 60*(1-t));
  const g = Math.round(200*(1-t) + 180*t);
  const b = 60;
  return `rgb(${{r}},${{g}},${{b}})`;
}}

function zoneColor(v) {{
  if (v == null) return '#f5f5f5';
  const r = Math.round(220*(1-v) + 60*v);
  const g = Math.round(60*(1-v) + 180*v);
  return `rgb(${{r}},${{g}},60)`;
}}

function biasColor(v) {{
  if (v == null) return '#f5f5f5';
  const t = Math.max(0, Math.min(1, Math.abs(v) / 3));
  return `rgb(${{Math.round(255*t+240*(1-t))}},${{Math.round(180*(1-t)+180*t)}},180)`;
}}

function cellColor(v, metric) {{
  if (metric === 'zone_accuracy') return zoneColor(v);
  if (metric === 'bias') return biasColor(v);
  return maeColor(v);
}}

// ── Heatmap ──────────────────────────────────────────────────────────────
function renderHeatmap() {{
  const method = getMethod();
  const metric = getMetric();
  const selectedRuns = getSelectedRuns();
  let html = '<table><thead><tr><th class="run-col">Run</th>';
  MODELS.forEach(m => {{ html += `<th>${{MODEL_LABELS[m]}}</th>`; }});
  html += '</tr></thead><tbody>';

  (selectedRuns.length ? selectedRuns : RUNS).forEach(run => {{
    html += `<tr><td class="run-label">${{run}}</td>`;
    MODELS.forEach(m => {{
      const cell = DATA[run]?.[m]?.[method];
      if (!cell) {{
        html += '<td class="na">no data</td>';
      }} else {{
        const v = cell[metric];
        const bg = cellColor(v, metric);
        const textColor = v != null ? '#111' : '#aaa';
        const extra = metric === 'mae' || metric === 'rmse'
          ? `<div class="metric-line">Zone: ${{fmt(cell.zone_accuracy,'zone_accuracy')}}  n=${{cell.n}}</div>`
          : `<div class="metric-line">MAE: ${{fmt(cell.mae,'mae')}}  n=${{cell.n}}</div>`;
        html += `<td class="cell" style="background:${{bg}};color:${{textColor}}">
                   ${{fmt(v, metric)}}${{extra}}</td>`;
      }}
    }});
    html += '</tr>';
  }});
  html += '</tbody></table>';
  document.getElementById('heatmap-container').innerHTML = html;
}}

// ── Horizon charts ────────────────────────────────────────────────────────
const CHART_COLORS = ['#3b82f6','#10b981','#f59e0b','#ef4444'];

function renderHorizonCharts() {{
  const method = getMethod();
  const metric = getMetric();
  const selectedRuns = getSelectedRuns();
  const activeRuns = selectedRuns.length ? selectedRuns : RUNS;

  // Destroy old charts
  Object.values(charts).forEach(c => c.destroy());
  charts = {{}};

  const wrap = document.getElementById('horizon-charts');
  wrap.innerHTML = '';

  // Find the max number of forecast weeks any active run has
  let maxWeeks = 0;
  activeRuns.forEach(run => {{
    MODELS.forEach(m => {{
      const cell = DATA[run]?.[m]?.[method];
      if (cell) maxWeeks = Math.max(maxWeeks, Object.keys(cell.per_week || {{}}).length);
    }});
  }});
  if (maxWeeks === 0) {{ wrap.innerHTML = '<p class="no-data">No week-level data.</p>'; return; }}

  // x-axis: relative offsets Week 1, Week 2, ...
  const weekLabels = Array.from({{length: maxWeeks}}, (_, i) => `Week ${{i + 1}}`);

  // One chart per model
  MODELS.forEach((model, mi) => {{
    const div = document.createElement('div');
    div.className = 'chart-box';
    div.innerHTML = `<h3>${{MODEL_LABELS[model]}}</h3><canvas id="chart-${{model}}"></canvas>`;
    wrap.appendChild(div);

    const datasets = activeRuns.map((run, ri) => {{
      const cell = DATA[run]?.[model]?.[method];
      const sortedWeeks = Object.keys(cell?.per_week || {{}}).sort();
      const vals = weekLabels.map((_, i) => {{
        const w = sortedWeeks[i];
        return w ? (cell.per_week[w]?.[metric] ?? null) : null;
      }});
      return {{
        label: run,
        data: vals,
        borderColor: CHART_COLORS[ri % CHART_COLORS.length],
        backgroundColor: CHART_COLORS[ri % CHART_COLORS.length] + '22',
        tension: 0.3,
        pointRadius: 4,
        spanGaps: false,
      }};
    }});

    const isZone = metric === 'zone_accuracy';
    const ctx = document.getElementById(`chart-${{model}}`);
    charts[model] = new Chart(ctx, {{
      type: 'line',
      data: {{ labels: weekLabels, datasets }},
      options: {{
        responsive: true,
        plugins: {{
          legend: {{ position: 'bottom', labels: {{ font: {{ size: 11 }}, boxWidth: 12 }} }},
          title: {{ display: false }},
        }},
        scales: {{
          y: {{
            min: isZone ? 0 : undefined,
            max: isZone ? 1 : undefined,
            title: {{ display: true, text: metricLabel(metric), font: {{ size: 11 }} }},
            ticks: {{ callback: v => isZone ? (v*100).toFixed(0)+'%' : v.toFixed(2), font: {{ size: 11 }} }},
          }},
          x: {{ ticks: {{ font: {{ size: 10 }}, maxRotation: 45 }} }},
        }},
      }},
    }});
  }});
}}

// ── District table ────────────────────────────────────────────────────────
let sortCol = 'mae', sortDir = 1;

function renderDistrictTable() {{
  const run = getDistrictRun();
  const model = getDistrictModel();
  const method = getMethod();
  const cell = DATA[run]?.[model]?.[method];
  const wrap = document.getElementById('district-table-wrap');

  if (!cell?.per_district) {{
    wrap.innerHTML = '<p class="no-data">No data for this run / model combination.</p>';
    return;
  }}

  let rows = Object.entries(cell.per_district).map(([d, m]) => ({{...m, district: d}}));
  rows.sort((a, b) => ((a[sortCol]??9999) - (b[sortCol]??9999)) * sortDir);

  const cols = [
    {{ key: 'district', label: 'District', fmt: v => v }},
    {{ key: 'n',        label: 'N weeks',  fmt: v => v }},
    {{ key: 'mae',      label: 'MAE',      fmt: v => v?.toFixed(3) ?? '—' }},
    {{ key: 'rmse',     label: 'RMSE',     fmt: v => v?.toFixed(3) ?? '—' }},
    {{ key: 'nrmse',    label: 'NRMSE',    fmt: v => v?.toFixed(3) ?? '—' }},
    {{ key: 'zone_accuracy', label: 'Zone Acc', fmt: v => v != null ? (v*100).toFixed(1)+'%' : '—' }},
    {{ key: 'bias',     label: 'Bias',     fmt: v => v?.toFixed(3) ?? '—' }},
  ];

  let html = '<table><thead><tr>';
  cols.forEach(c => {{
    const arrow = sortCol === c.key ? (sortDir === 1 ? ' ▲' : ' ▼') : '';
    html += `<th class="sortable" data-col="${{c.key}}">${{c.label}}${{arrow}}</th>`;
  }});
  html += '</tr></thead><tbody>';

  rows.forEach(row => {{
    const bg = cellColor(row.mae, 'mae');
    html += '<tr>';
    cols.forEach(c => {{
      const style = c.key === 'mae' ? `style="background:${{bg}}"` : '';
      html += `<td ${{style}}>${{c.fmt(row[c.key])}}</td>`;
    }});
    html += '</tr>';
  }});
  html += '</tbody></table>';
  wrap.innerHTML = html;

  wrap.querySelectorAll('.sortable').forEach(th => {{
    th.addEventListener('click', () => {{
      if (sortCol === th.dataset.col) sortDir *= -1;
      else {{ sortCol = th.dataset.col; sortDir = 1; }}
      renderDistrictTable();
    }});
  }});
}}

// ── Init ──────────────────────────────────────────────────────────────────
function renderAll() {{
  renderHeatmap();
  renderHorizonCharts();
  renderDistrictTable();
}}

// Populate run checkboxes (all checked by default)
const runCheckboxWrap = document.getElementById('run-checkboxes');
RUNS.forEach(r => {{
  const lbl = document.createElement('label');
  const cb = document.createElement('input');
  cb.type = 'checkbox'; cb.value = r; cb.checked = true;
  cb.addEventListener('change', renderAll);
  lbl.appendChild(cb);
  lbl.appendChild(document.createTextNode(' ' + r));
  runCheckboxWrap.appendChild(lbl);
}});

// Populate district run selector
const runSel = document.getElementById('run-select');
RUNS.forEach(r => {{ const o = document.createElement('option'); o.value = r; o.text = r; runSel.appendChild(o); }});

document.querySelectorAll('input[name=method]').forEach(el => el.addEventListener('change', renderAll));
document.getElementById('metric-select').addEventListener('change', renderAll);
document.getElementById('run-select').addEventListener('change', renderDistrictTable);
document.getElementById('district-model-select').addEventListener('change', renderDistrictTable);

renderAll();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Evaluate dengue forecast accuracy and generate an HTML report.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  # Evaluate a single run by name (looks in artifacts/ap/<name>/)
  uv run python scripts/compute_backtest_metrics.py --runs backtest-march

  # Evaluate multiple runs side by side
  uv run python scripts/compute_backtest_metrics.py --runs backtest-march march-01-run

  # Evaluate by path (relative or absolute)
  uv run python scripts/compute_backtest_metrics.py --runs artifacts/ap/backtest-march

  # Only look at specific models
  uv run python scripts/compute_backtest_metrics.py --runs backtest-march --models nbr xgb

  # Only look at one threshold method
  uv run python scripts/compute_backtest_metrics.py --runs backtest-march --method historical

  # Write output to a custom directory
  uv run python scripts/compute_backtest_metrics.py --runs backtest-march --output /tmp/eval
        """,
    )
    parser.add_argument(
        "--runs",
        nargs="+",
        required=True,
        metavar="RUN",
        help="One or more run names (e.g. backtest-march) or paths (e.g. artifacts/ap/backtest-march)",
    )
    parser.add_argument(
        "--models",
        nargs="+",
        metavar="MODEL",
        choices=list(MODEL_FILE_PATTERNS.keys()),
        help="Models to evaluate: nbr rf xgb ensemble (default: all)",
    )
    parser.add_argument(
        "--method",
        metavar="METHOD",
        choices=THRESHOLD_METHODS,
        help="Threshold method to evaluate: historical or previousNweeks (default: both)",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        metavar="DIR",
        help="Output directory (default: metrics_output/)",
    )
    args = parser.parse_args()

    # Apply CLI filters to the module-level constants so all helpers respect them
    if args.models:
        for k in list(MODEL_FILE_PATTERNS.keys()):
            if k not in args.models:
                del MODEL_FILE_PATTERNS[k]
    if args.method:
        THRESHOLD_METHODS[:] = [args.method]

    run_dirs = []
    for r in args.runs:
        p = Path(r)
        if p.is_absolute() or p.exists():
            run_dirs.append(p)
        else:
            run_dirs.append(ARTIFACTS_ROOT / r)

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    print("\nLooking for actuals data...")
    cases_csv, actuals_date, source_kind = find_best_cases_csv()
    if cases_csv is None:
        print(
            "ERROR: No case data found to use as actuals.\n"
            "  Expected: prepared_data/district/cases_daily.csv\n"
            "  Fix: run the prep pipeline first, or download prepared_data from S3:\n"
            "       python scripts/sync_data.py download --only prepared"
        )
        return
    try:
        display_path = cases_csv.relative_to(Path(__file__).parent.parent)
    except ValueError:
        display_path = cases_csv
    print(f"  Found: {display_path} (through {actuals_date})\n")

    actuals = load_actuals(cases_csv, source_kind)

    all_records = []
    for run_dir in run_dirs:
        if not run_dir.exists():
            print(
                f"  ERROR: run not found — {run_dir}\n"
                f"         Run names are looked up under artifacts/ap/. "
                f"Check the name with: ls artifacts/ap/"
            )
            continue
        print(f"Evaluating: {run_dir.name}")
        all_records.extend(evaluate_run(run_dir, actuals))

    if not all_records:
        print(
            "\nNo metrics could be computed. Common reasons:\n"
            "  1. The run date is too recent — predictions cover future weeks that have no actuals yet.\n"
            "     Fix: use a run_date at least 5–6 weeks before today.\n"
            "  2. The run's results/ folder has no prediction CSVs.\n"
            "     Check: ls artifacts/ap/<run-id>/results/\n"
            "  3. The model was filtered out with --models.\n"
            "     Check: re-run without --models to see all models."
        )
        return

    # CSVs
    summary_df = flatten_for_csv(all_records)
    summary_df.drop(columns=["per_week", "per_district"], errors="ignore").to_csv(
        output_dir / "metrics_summary.csv", index=False
    )
    flatten_for_csv(all_records, "per_week").to_csv(
        output_dir / "metrics_per_week.csv", index=False
    )
    flatten_for_csv(all_records, "per_district").to_csv(
        output_dir / "metrics_per_district.csv", index=False
    )

    # JSON
    json_data = build_json(all_records)
    with open(output_dir / "metrics.json", "w") as f:
        json.dump(json_data, f, indent=2)

    # HTML
    html = build_html_report(json_data, actuals_date)
    html_path = output_dir / "metrics_report.html"
    html_path.write_text(html)

    print(f"\nOutputs written to {output_dir}/")
    print("  metrics_report.html     ← open this in your browser")
    print("  metrics_summary.csv")
    print("  metrics_per_week.csv")
    print("  metrics_per_district.csv")
    print("  metrics.json")

    print("\n── Summary ──────────────────────────────────────────────")
    print(
        f"  {'Run':<22} {'Model':<10} {'Method':<16} {'MAE':>6} {'RMSE':>6} {'NRMSE':>7} {'ZoneAcc':>8}"
    )
    print(f"  {'-'*22} {'-'*10} {'-'*16} {'-'*6} {'-'*6} {'-'*7} {'-'*8}")
    for run, models in json_data.items():
        for model, methods in models.items():
            for method, m in methods.items():
                za = f"{m['zone_accuracy']*100:.1f}%" if m["zone_accuracy"] else "  —"
                nr = f"{m['nrmse']:.3f}" if m.get("nrmse") is not None else "  —"
                print(
                    f"  {run:<22} {model:<10} {method:<16} "
                    f"{m['mae']:>6.3f} {m['rmse']:>6.3f} {nr:>7} {za:>8}"
                )


if __name__ == "__main__":
    main()

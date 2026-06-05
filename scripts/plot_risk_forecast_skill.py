"""Risk-forecast skill heatmaps — categorical risk levels, model vs naive persistence.

One panel grid per categorical skill metric × zoning scheme; each panel is a
model-variant × lead-time heatmap. Skill = (model − persistence) / (1 − persistence):
> 0 beats persistence, 0 ties, 1 is perfect. Cells with no defined skill show "—".

Schemes, model variants and lead times are read from the input CSV — the script is
not tied to any particular model set. Edit the CONFIG block below to change display
order, labels, colours, resolution wording or the alert level; unknown variants/schemes
still plot (appended in CSV order, labelled by their raw id).

Input is the skill CSV written by the categorical backtest, with columns:
    scheme, variant, lead, n, <one column per metric in METRICS>
Blank metric cells are read as undefined skill and render as "—". Malformed input
(missing key columns, no metric columns, duplicate keys, empty after dropping blanks)
fails loudly with a clear message rather than a cryptic matplotlib error.

Usage:
    uv run python scripts/plot_risk_forecast_skill.py [--skill-csv PATH] [--out PATH]
"""

from __future__ import annotations

import argparse
import textwrap
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
DEFAULT_CSV = REPO / "artifacts/nonbr_experiment/eval_categorical/skill_categorical_by_scheme_variant_lead.csv"
DEFAULT_OUT = REPO / "docs/eval_figures/risk_forecast_skill.png"

# ─────────────────────────── CONFIG (edit here) ───────────────────────────
TITLE = "Risk-forecast skill vs naive persistence"
SUBTITLE = "AP districts, 2024"          # dataset / run descriptor shown in the title
SPATIAL_UNIT = "district"                # what one region of the data is: district / mandal / …
PERIOD, PERIOD_PLURAL = "week", "weeks"  # lead-time / cadence unit (singular, plural)
CMAP, VMIN, VMAX = "RdYlGn", -1.0, 1.0

# Preferred display order. Anything present in the CSV but not listed is appended
# in CSV order, so adding a model or scheme needs no code change here.
SCHEME_ORDER = ["WHO-historical", "WHO-prevNweeks", "Percentile", "ICMR"]
VARIANT_LABELS = {                       # raw id -> pretty label (fallback: prettified id)
    "nbr": "Negative\nBinomial",
    "rf_xgb": "Random Forest\n+ XGBoost",
    "ensemble": "Ensemble",
}

# Alert level the input `skill_alert_f1` was ALREADY computed at by the backtest.
# This only labels the figure — it does NOT re-threshold anything (the CSV carries the
# precomputed F1 and no cutoff metadata). It must equal the backtest's alert level, or
# the label will misdescribe the data.
ALERT_MIN_LEVEL = 3
# Skill metrics to show as rows (column in CSV -> row label). Missing columns are
# skipped, so the grid adapts to whatever the backtest wrote.
METRICS = [
    ("skill_exact", "Exact risk-level skill"),
    ("skill_within1", "Within-one-level skill"),
    ("skill_alert_f1", f"Alert-detection skill  (F1, Risk Level ≥ {ALERT_MIN_LEVEL})"),
]

# Risk-level ladder for the stand-alone bottom legend (number, name, colour).
LEVELS = [(1, "Low", "#1a9850"), (2, "Caution", "#fee08b"),
          (3, "High", "#fc8d59"), (4, "Critical", "#d73027")]
# Concrete case cut the alert level maps to in each scheme (keyed by ALERT_MIN_LEVEL),
# so "≥ N" is unambiguous per panel rather than implying one scheme's SD band is THE cut.
ALERT_CUTS = {
    3: "WHO ≥ Mean + 1·SD   ·   Percentile > P75   ·   ICMR top half (A1 + A2)",
    4: "WHO ≥ Mean + 2·SD   ·   Percentile > P90   ·   ICMR top quartile (A1)",
}
# Shared subplot margins, reused for colorbar/row-label geometry so they stay aligned.
MARGINS = dict(left=0.10, right=0.90, top=0.92, bottom=0.20, hspace=0.28, wspace=0.10)
# ───────────────────────────────────────────────────────────────────────────


def _ordered(values, preferred):
    """Unique values in `preferred` order, with any extras appended in CSV order."""
    present = list(dict.fromkeys(values))
    return [v for v in preferred if v in present] + [v for v in present if v not in preferred]


def _variant_label(v):
    return VARIANT_LABELS.get(v) or textwrap.fill(str(v).replace("_", " ").title(), 14)


def _alert_level_names():
    names = [name for num, name, _ in LEVELS if num >= ALERT_MIN_LEVEL]
    return " + ".join(names)


def _grid(df, scheme, variants, leads, col):
    sub = df[df.scheme == scheme]
    lut = {(v, L): val for v, L, val in zip(sub.variant, sub.lead, sub[col])}
    return np.array([[lut.get((v, L), np.nan) for L in leads] for v in variants])


def _numeric_or_fail(series, label):
    """Coerce to numeric. Blank/NA stays NaN (a legitimately-undefined value), but a
    non-blank value that won't parse — or a non-finite inf — is a data error, so raise."""
    if pd.api.types.is_numeric_dtype(series):
        num = series.astype(float)
    else:
        raw = series.astype("string").str.strip()
        num = pd.to_numeric(raw, errors="coerce")
        bad = raw.notna() & (raw != "") & num.isna()
        if bad.any():
            raise ValueError(f"{label} has non-numeric value(s): {sorted(raw[bad].unique())}")
    if np.isinf(num).any():
        raise ValueError(f"{label} has non-finite (inf) value(s)")
    return num


def _load(path):
    """Read + validate the skill CSV. Returns (df, metric_cols). Distinguishes blank
    cells (dropped or rendered '—') from malformed values (fail loudly)."""
    if not path.exists():
        raise FileNotFoundError(f"skill CSV not found: {path}")
    try:
        df = pd.read_csv(path)
    except pd.errors.EmptyDataError as e:
        raise ValueError(f"skill CSV is empty (no header/columns): {path}") from e
    missing_keys = {"scheme", "variant", "lead"} - set(df.columns)
    if missing_keys:
        raise ValueError(f"skill CSV missing required column(s): {sorted(missing_keys)}")
    metric_cols = [c for c, _ in METRICS if c in df.columns]
    if not metric_cols:
        raise ValueError(
            f"skill CSV has none of the expected metric columns "
            f"{[c for c, _ in METRICS]}; found {list(df.columns)}"
        )

    # String keys: strip whitespace; whitespace-only becomes a blank (NA) key.
    for c in ("scheme", "variant"):
        s = df[c].astype("string").str.strip()
        df[c] = s.mask(s == "", pd.NA)

    # Lead: blank -> drop; non-numeric or non-integer -> data error.
    lead = _numeric_or_fail(df["lead"], "skill CSV 'lead'")
    noninteger = lead.notna() & (lead != lead.round())
    if noninteger.any():
        bad = sorted(float(x) for x in lead[noninteger].unique())
        raise ValueError(f"skill CSV has non-integer lead value(s): {bad}")
    df["lead"] = lead

    # Metrics: blank -> NaN -> rendered '—'; malformed non-blank value -> data error.
    for c in metric_cols:
        df[c] = _numeric_or_fail(df[c], f"skill CSV column {c!r}")

    blank = df[["scheme", "variant", "lead"]].isna().any(axis=1)
    if blank.any():
        print(f"warning: dropping {int(blank.sum())} row(s) with blank scheme/variant/lead")
        df = df[~blank].copy()
    if df.empty:
        raise ValueError(f"skill CSV has no usable rows: {path}")
    df["lead"] = df["lead"].astype(int)

    dup = df.duplicated(["scheme", "variant", "lead"])
    if dup.any():
        raise ValueError(
            f"skill CSV has {int(dup.sum())} duplicate (scheme,variant,lead) row(s); "
            "each combination must be unique"
        )
    return df, metric_cols


def _draw_legend(fig, fig_w_in):
    """Stand-alone strip: the risk-level ladder (so '≥ N' is self-explanatory) plus what
    Skill and Alert mean. Right-side text wraps to the figure width so it never truncates."""
    ax = fig.add_axes((MARGINS["left"], 0.02, MARGINS["right"] - MARGINS["left"], 0.11))
    ax.axis("off")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)

    w, y0, h = 0.066, 0.55, 0.40
    alert_x = []
    for k, (num, name, colour) in enumerate(LEVELS):
        x = k * w
        ax.add_patch(Rectangle((x, y0), w * 0.9, h, facecolor=colour, edgecolor="white"))
        ax.text(x + w * 0.45, y0 + h / 2, f"{num}  {name}", ha="center", va="center",
                fontsize=9.8, fontweight="bold", color="black")
        if num >= ALERT_MIN_LEVEL:
            alert_x += [x, x + w * 0.9]
    if alert_x:
        lo, hi = min(alert_x), max(alert_x)
        ax.plot([lo, hi], [y0 - 0.18, y0 - 0.18], color="#333333", lw=1.3, clip_on=False)
        ax.text((lo + hi) / 2, y0 - 0.42, f"Alert = Risk Level ≥ {ALERT_MIN_LEVEL}",
                ha="center", va="top", fontsize=9.5, color="#333333")

    tx = 0.30
    avail_in = (MARGINS["right"] - MARGINS["left"]) * fig_w_in * (1 - tx)
    wrap = max(60, int(avail_in / 0.062))       # ≈ char capacity at 10pt for this width
    blocks = [
        f"Risk Level — an ordinal 1–{len(LEVELS)} ladder (1 lowest  →  {len(LEVELS)} most extreme); "
        f"the reference differs by scheme: each {SPATIAL_UNIT}'s own history for WHO / Percentile, "
        f"cross-{SPATIAL_UNIT} ranking that period for ICMR.",
    ]
    cuts = ALERT_CUTS.get(ALERT_MIN_LEVEL)
    if cuts:
        blocks.append(
            f"Alert (Risk Level ≥ {ALERT_MIN_LEVEL} = {_alert_level_names()}) by scheme:   {cuts}."
        )
    blocks.append(
        f"Skill = (model − persistence) / (1 − persistence):  > 0 beats persistence "
        f"(last observed {PERIOD} held flat),  0 ties,  1 perfect."
    )
    lines = [wl for b in blocks for wl in (textwrap.wrap(b, wrap) or [b])]
    y = 0.92
    for line in lines:
        ax.text(tx, y, line, ha="left", va="center", fontsize=10.0, color="#444444")
        y -= 0.30


def main() -> None:
    ap = argparse.ArgumentParser(description="Render risk-forecast skill heatmaps.")
    ap.add_argument("--skill-csv", type=Path, default=DEFAULT_CSV)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    if not 1 <= ALERT_MIN_LEVEL <= len(LEVELS):
        raise ValueError(f"ALERT_MIN_LEVEL must be in 1..{len(LEVELS)}, got {ALERT_MIN_LEVEL}")

    df, metric_cols = _load(args.skill_csv)
    schemes = _ordered(df["scheme"], SCHEME_ORDER)
    variants = _ordered(df["variant"], list(VARIANT_LABELS))
    leads = sorted(df["lead"].unique())
    metrics = [(c, lbl) for c, lbl in METRICS if c in metric_cols]
    vlabels = [_variant_label(v) for v in variants]

    present = set(zip(df.scheme, df.variant, df.lead))
    absent = [t for t in ((s, v, L) for s in schemes for v in variants for L in leads)
              if t not in present]
    if absent:
        shown = ", ".join(map(str, absent[:5])) + (" …" if len(absent) > 5 else "")
        print(f"note: {len(absent)} (scheme,variant,lead) cell(s) absent, shown as '—': {shown}")

    nrow, ncol = len(metrics), len(schemes)
    fig_w = 5 * ncol
    fig, axes = plt.subplots(nrow, ncol, figsize=(fig_w, 4.0 * nrow + 1.3), squeeze=False)
    fig.subplots_adjust(**MARGINS)

    im = None
    for r, (col, _) in enumerate(metrics):
        for c, scheme in enumerate(schemes):
            ax = axes[r][c]
            grid = _grid(df, scheme, variants, leads, col)
            im = ax.imshow(grid, cmap=CMAP, vmin=VMIN, vmax=VMAX, aspect="auto")
            ax.set_xticks(range(len(leads)), leads, fontsize=12)
            ax.set_yticks(range(len(variants)), vlabels if c == 0 else [], fontsize=12)
            if r == 0:
                ax.set_title(scheme, fontsize=14)
            if r == nrow - 1:
                ax.set_xlabel(f"Lead time ({PERIOD_PLURAL})", fontsize=12)
            for i in range(len(variants)):
                for j in range(len(leads)):
                    v = grid[i, j]
                    ax.text(j, i, "—" if np.isnan(v) else f"{v:.2f}",
                            ha="center", va="center", fontsize=12, fontweight="bold")

    # Row (metric) labels: placed from each row's actual geometry, robust to ncol.
    for r, (_, mlabel) in enumerate(metrics):
        pos = axes[r][0].get_position()
        fig.text(0.022, (pos.y0 + pos.y1) / 2, mlabel, rotation=90,
                 ha="center", va="center", fontsize=13, fontweight="bold")

    fig.suptitle(f"{TITLE}  ({SUBTITLE})", fontsize=16, y=0.965)
    cax = fig.add_axes((MARGINS["right"] + 0.015, MARGINS["bottom"], 0.012,
                        MARGINS["top"] - MARGINS["bottom"]))
    fig.colorbar(im, cax=cax, label="skill score")
    _draw_legend(fig, fig_w)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.out, dpi=130, bbox_inches="tight")
    print(f"saved {args.out}")


if __name__ == "__main__":
    main()

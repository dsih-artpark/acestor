"""Risk-level definition table for the dengue backtest figures.

Reference PNG defining the four risk levels (1 Low … 4 Critical) under each zoning
scheme, plus what counts as an "alert". The schemes are resolution-agnostic: WHO and
Percentile score each region against its own history, ICMR ranks regions against each
other per period — so the same definitions hold at any spatial unit (district, mandal)
and any cadence. Set SPATIAL_UNIT / PERIOD / MIN_HISTORY_PERIODS in the CONFIG block to
match the run; nothing else is tied to a particular resolution.

Definitions mirror the code that produced the metrics:
  WHO        — Level = 1 + 1[v≥Mean] + 1[v≥Mean+SD] + 1[v≥Mean+2SD]
  Percentile — P50/P75/P90 of the region's own history up to the forecast origin,
               linear interpolation (no rounding); strict '>' band test.
  ICMR       — cross-sectional quartile strata across regions each period.

Usage:
    uv run python scripts/plot_risk_level_definitions.py [--out PATH]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

REPO = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO / "docs/eval_figures/risk_level_definitions.png"

# ─────────────────────────── CONFIG (edit per run) ───────────────────────────
SPATIAL_UNIT, SPATIAL_UNIT_PLURAL = "district", "districts"  # what one row of the data is
PERIOD, PERIOD_PLURAL = "week", "weeks"  # temporal cadence (singular, plural)
MIN_HISTORY_PERIODS = 20      # Percentile guardrail: minimum history before a channel is usable
ALERT_MIN_LEVEL = 3           # alert = Risk Level ≥ this
HISTORICAL_N_YEARS = 4        # WHO-historical baseline: same-season cases, previous N years
PREV_N_PERIODS = 4            # WHO-prevNweeks baseline: previous N periods
# ─────────────────────────────────────────────────────────────────────────────

# Level, label, colour, WHO band, Percentile band, ICMR stratum.
# WHO uses '≥' at the lower edge; Percentile uses strict '>', so a value sitting
# exactly on a cut point falls in the lower level.
ROWS = [
    ("4", "Critical", "#d73027",
     "prediction ≥ Mean + 2·SD",
     "prediction > P90",
     f"A1 — top quartile of distinct predicted values that {PERIOD}"),
    ("3", "High", "#fc8d59",
     "Mean + 1·SD ≤ prediction < Mean + 2·SD",
     "P75 < prediction ≤ P90",
     "A2 — second quartile"),
    ("2", "Caution", "#fee08b",
     "Mean ≤ prediction < Mean + 1·SD",
     "P50 < prediction ≤ P75",
     "A3 — third quartile"),
    ("1", "Low", "#1a9850",
     "prediction < Mean",
     "prediction ≤ P50",
     "A4 — bottom quartile"),
    ("—", "No level / grey", "#d9d9d9",
     "degenerate: Mean = 0 and SD = 0",
     f"< {MIN_HISTORY_PERIODS} {PERIOD_PLURAL} history, or P90 ≤ P50",
     f"Σ predicted cases < 10 that {PERIOD}"),
]
COL_HEADERS = [
    "Risk level",
    "WHO  (own-history bands, absolute)",
    "Percentile  (endemic channel, absolute)",
    f"ICMR  (quartile strata, relative per {PERIOD})",
]


def _alert_level_names():
    """Names of the levels that count as an alert (Risk Level ≥ ALERT_MIN_LEVEL),
    derived from ROWS so the note never goes stale when ALERT_MIN_LEVEL changes."""
    names = [name for lvl, name, *_ in ROWS if lvl.isdigit() and int(lvl) >= ALERT_MIN_LEVEL]
    return " + ".join(reversed(names))  # ROWS is high→low; read low→high


NOTE = (
    f"Alert = Risk Level ≥ {ALERT_MIN_LEVEL} ({_alert_level_names()}) — a deliberately sensitive "
    f"early-warning cut; the classic epidemic threshold is Level 4 (≈ Mean + 2·SD).\n"
    f"Thresholds are recomputed each {PERIOD}.  WHO baseline: same-season cases over the previous "
    f"{HISTORICAL_N_YEARS} years (historical), or the previous {PREV_N_PERIODS} {PERIOD_PLURAL} (prevNweeks).\n"
    f"WHO and Percentile score each {SPATIAL_UNIT} against its own history "
    f"(absolute; Percentile cut points use linear interpolation, no rounding);  "
    f"ICMR ranks {SPATIAL_UNIT_PLURAL} against each other each {PERIOD} (relative)."
)


def main() -> None:
    ap = argparse.ArgumentParser(description="Render the risk-level definition table.")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    levels = [int(lvl) for lvl, *_ in ROWS if lvl.isdigit()]
    if not min(levels) <= ALERT_MIN_LEVEL <= max(levels):
        raise ValueError(
            f"ALERT_MIN_LEVEL must be in {min(levels)}..{max(levels)}, got {ALERT_MIN_LEVEL}"
        )

    fig, ax = plt.subplots(figsize=(17, 3.6))
    ax.axis("off")

    cells = [[f"{lvl}  {name}", who, pct, icmr] for lvl, name, _, who, pct, icmr in ROWS]
    cell_colours = [[colour, "white", "white", "white"] for _, _, colour, *_ in ROWS]

    tbl = ax.table(
        cellText=cells,
        colLabels=COL_HEADERS,
        cellColours=cell_colours,
        colColours=["#404040"] * 4,
        cellLoc="left", colLoc="left", loc="center",
    )
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(10.5)
    tbl.scale(1, 2.1)
    for (r, _), cell in tbl.get_celld().items():
        if r == 0:
            cell.get_text().set_color("white")
            cell.get_text().set_fontweight("bold")
        cell.set_edgecolor("white")
    tbl.auto_set_column_width([0, 1, 2, 3])

    ax.set_title("Dengue risk levels — what the numbers mean", fontsize=14, pad=14)
    fig.text(0.5, -0.04, NOTE, ha="center", va="top", fontsize=10.5, color="#333333",
             linespacing=1.5)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.out, dpi=130, bbox_inches="tight")
    print(f"saved {args.out}")


if __name__ == "__main__":
    main()

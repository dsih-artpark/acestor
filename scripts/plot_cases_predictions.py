"""
Plot weekly dengue case time series (AP) with predictions overlaid.
One subplot per district; predictions from different run_dates shown in distinct colors.
"""

import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]

# ── 1. Load actual cases (weekly, already aggregated) ─────────────────────────
cases = pd.read_csv(BASE / "artifacts/ap/123/datasets/cases_district_sampled.csv")
cases.columns = ["regionID", "week_start", "actual"]
cases["week_start"] = pd.to_datetime(cases["week_start"])

# ── 2. Load predictions ────────────────────────────────────────────────────────
pred_files = {
    "2026-02-10": BASE
    / "artifacts/ap/123/results/Predictions_Feb - Mar 2026_District_20260210.csv",
    "2026-04-22": BASE
    / "artifacts/ap/123/results/Predictions_Feb - Mar 2026_20260422.csv",
}

pred_dfs = []
for run_date, fpath in pred_files.items():
    df = pd.read_csv(fpath)
    # Deduplicate rows with same regionID+week (thresholdMethod creates duplicates; prediction value is identical)
    df = df.drop_duplicates(subset=["regionID", "startDatePredictedWeek"])
    df = df[["regionID", "startDatePredictedWeek", "prediction"]].copy()
    df["run_date"] = run_date
    pred_dfs.append(df)

preds = pd.concat(pred_dfs, ignore_index=True)
preds["startDatePredictedWeek"] = pd.to_datetime(preds["startDatePredictedWeek"])

# ── 3. District code → name map ────────────────────────────────────────────────
district_names = {
    502: "Ananthapuramu",
    503: "Chittoor",
    504: "Y.S.R. Kadapa",
    505: "East Godavari",
    506: "Guntur",
    510: "Krishna",
    511: "Kurnool",
    515: "Sri Potti Sriramulu Nellore",
    517: "Prakasam",
    519: "Srikakulam",
    520: "Visakhapatnam",
    521: "Vizianagaram",
    523: "West Godavari",
    743: "Parvathipuram Manyam",
    744: "Anakapalli",
    745: "Alluri Sitharama Raju",
    746: "Kakinada",
    747: "Dr. B.R. Ambedkar Konaseema",
    748: "Eluru",
    749: "NTR",
    750: "Bapatla",
    751: "Palnadu",
    752: "Tirupati",
    753: "Annamayya",
    754: "Sri Sathya Sai",
    755: "Nandyal",
    790: "Markapuram",
    791: "Polavaram",
}
id_to_name = {f"district_{k}": v for k, v in district_names.items()}

districts = sorted(cases["regionID"].unique())

# ── 4. Colour map for run_dates ────────────────────────────────────────────────
run_dates = sorted(preds["run_date"].unique())
pred_colors = {
    rd: c
    for rd, c in zip(run_dates, ["tab:orange", "tab:red", "tab:purple", "tab:brown"])
}

# ── 5. Plot ───────────────────────────────────────────────────────────────────
ncols = 4
nrows = int(np.ceil(len(districts) / ncols))
fig, axes = plt.subplots(
    nrows, ncols, figsize=(22, nrows * 3.5), constrained_layout=True
)
axes_flat = axes.flatten()

for i, region in enumerate(districts):
    ax = axes_flat[i]
    title = id_to_name.get(region, region)

    # Actual cases — full history
    act = cases[cases["regionID"] == region].sort_values("week_start")
    ax.plot(
        act["week_start"],
        act["actual"],
        color="steelblue",
        linewidth=1.2,
        label="Actual cases",
        zorder=3,
    )

    # Predictions per run_date
    for rd in run_dates:
        pr = preds[
            (preds["regionID"] == region) & (preds["run_date"] == rd)
        ].sort_values("startDatePredictedWeek")
        if pr.empty:
            continue
        ax.plot(
            pr["startDatePredictedWeek"],
            pr["prediction"],
            color=pred_colors[rd],
            linewidth=1.8,
            linestyle="--",
            marker="o",
            markersize=4,
            label=f"Pred run {rd}",
            zorder=4,
        )

    ax.set_title(title, fontsize=8, fontweight="bold")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=6))
    plt.setp(ax.get_xticklabels(), rotation=30, ha="right", fontsize=6)
    ax.tick_params(axis="y", labelsize=6)
    ax.set_xlim(pd.Timestamp("2021-09-01"), pd.Timestamp("2026-05-01"))
    ax.grid(True, alpha=0.3, linewidth=0.5)

# Hide unused subplots
for j in range(len(districts), len(axes_flat)):
    axes_flat[j].set_visible(False)

# Shared legend
handles, labels = axes_flat[0].get_legend_handles_labels()
fig.legend(
    handles,
    labels,
    loc="lower right",
    fontsize=9,
    framealpha=0.9,
    ncol=len(run_dates) + 1,
)

fig.suptitle(
    "AP Dengue — Weekly Cases & Predictions by Run Date", fontsize=14, fontweight="bold"
)

out = BASE / "artifacts/ap/cases_predictions_timeseries.png"
fig.savefig(out, dpi=150, bbox_inches="tight")
print(f"Saved → {out}")
plt.show()

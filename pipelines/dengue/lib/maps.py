"""Map generation functions (choropleth risk maps)."""

from __future__ import annotations

import logging
import math
import os
from pathlib import Path

import geopandas as gpd
import matplotlib

log = logging.getLogger(__name__)

# Pipeline runner executes steps in worker threads; macOS GUI backend raises.
matplotlib.use("Agg")

import matplotlib.patches as mpatches  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

COLOR_MAPPING = {1: "green", 2: "yellow", 3: "orange", 4: "red", 0: "w"}
ZONE_LABEL = {1: "Low", 2: "Low Medium", 3: "Medium", 4: "High"}

REGION_LABEL = {
    "corp": "Corp",
    "zone": "Zone",
    "ward": "Ward",
    "district": "District",
    "subdistrict": "Subdistrict",
    "mandal": "Mandal",
}
MODEL_LABEL = {
    "negativeBinomialRegression": "Negative Binomial Regression",
    "ensembleModel": "Ensemble Model",
}
# Maps the short user-facing model name (used in configs as `report.primary`)
# to the full string that appears in the predictions CSV's `model` column.
MODEL_FULL_NAME = {
    "nbr": "negativeBinomialRegression",
    "xgb": "xgboostRegression",
    "rf": "randomForestRegression",
    "tse": "timeSeriesExtrapolation",
    "timesfm": "timesFoundationModel",
    "ensemble": "ensembleModel",
}
THRESHOLD_LABEL = {
    "historical": "Historical Thresholds",
    "previousNweeks": "Previous N-Weeks Thresholds",
}


def gen_plot(
    color_df: pd.DataFrame,
    *,
    region: str = "corp",
    model: str = "negativeBinomialRegression",
    threshold: str = "historical",
    thisdate: str,
    geojson_base: str,
    output_dir: str = "plots",
    figure_title: str = "Dengue risk map",
    run_date: str = "",
) -> str:
    """Generate a choropleth risk-map PNG and return its path."""
    label = REGION_LABEL.get(region, region).lower()
    geojson_folder = os.path.join(geojson_base, f"{label}s")
    if not os.path.isdir(geojson_folder):
        geojson_folder = os.path.join(geojson_base, label)
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    gdf_list = []
    no_prediction: list[str] = (
        []
    )  # in GeoJSON, not in predictions at all → white + hatch
    nan_zone: list[str] = (
        []
    )  # in predictions, zone is NaN (degenerate Mean=StdDev=0) → light gray
    zone_zero: list[str] = (
        []
    )  # in predictions, zone = 0 (no threshold pair matched) → white

    for fname in os.listdir(geojson_folder):
        if not fname.endswith(".geojson"):
            continue
        gdf = gpd.read_file(os.path.join(geojson_folder, fname))
        rname = os.path.splitext(fname)[0]
        if rname in color_df["regionID"].values:
            code = color_df.loc[color_df["regionID"] == rname, "predictionZone"].values[
                0
            ]
            if code is not None and not (isinstance(code, float) and math.isnan(code)):
                zone = int(code)
                gdf["color"] = COLOR_MAPPING.get(zone, "w")
                gdf["hatch"] = ""
                if zone == 0:
                    zone_zero.append(rname)
            else:
                gdf["color"] = "lightgray"
                gdf["hatch"] = ""
                nan_zone.append(rname)
        else:
            gdf["color"] = "w"
            gdf["hatch"] = "////"
            no_prediction.append(rname)
        gdf_list.append(gdf)

    ctx = f"[{region} | {thisdate} | {model} | {threshold}]"
    if no_prediction:
        log.warning(
            "generate_maps %s: %d region(s) have NO prediction → white/hatched "
            "(not in combined predictions CSV — check case data, weather coverage, or NBR/TSE drop): %s",
            ctx,
            len(no_prediction),
            no_prediction,
        )
    if nan_zone:
        log.warning(
            "generate_maps %s: %d region(s) have predictionZone=NaN → light gray "
            "(degenerate thresholds: Mean=0, StdDev=0 — historically zero reported cases): %s",
            ctx,
            len(nan_zone),
            nan_zone,
        )
    if zone_zero:
        log.warning(
            "generate_maps %s: %d region(s) have predictionZone=0 → white/no-hatch "
            "(prediction did not fall within any threshold pair — check threshold computation): %s",
            ctx,
            len(zone_zero),
            zone_zero,
        )

    if not gdf_list:
        return ""

    # Reproject every gdf onto the first one's CRS before concat. Two files
    # may both declare "WGS 84" yet fail the common-CRS check because their
    # WKT strings differ (files exported by different tools / at different
    # times). Normalising via to_crs() collapses those into one instance so
    # the concat succeeds without a fake CRS mismatch. Same fix that was
    # applied in dengue_prep/lib/ihip.py._load_geojson.
    target_crs = gdf_list[0].crs
    gdf_list = [g.to_crs(target_crs) for g in gdf_list]
    gdf_all = pd.concat(gdf_list, ignore_index=True)
    gdf_all = gpd.GeoDataFrame(gdf_all, geometry="geometry", crs=target_crs)
    gdf_dissolved = gdf_all.dissolve(by="name")

    fig, ax = plt.subplots(1, 1, figsize=(8, 10), dpi=140)
    for color in COLOR_MAPPING.values():
        subset = gdf_all[gdf_all["color"] == color]
        if len(subset) == 0:
            continue
        kw = {"ax": ax, "facecolor": color, "edgecolor": "none", "alpha": 0.5}
        if color == "w":
            kw["hatch"] = "////"
        subset.plot(**kw)

    gdf_dissolved.boundary.plot(ax=ax, edgecolor="black", linewidth=0.5, alpha=0.5)

    if region in ("corp", "zone"):
        for _, row in gdf_all.iterrows():
            c = row["geometry"].centroid
            ax.annotate(
                row["name"].title(),
                xy=(c.x, c.y),
                xytext=(0, 0),
                textcoords="offset points",
                horizontalalignment="center",
                fontsize=6,
                color="black",
            )

    ax.set_aspect("equal")

    patches = []
    for label, color in COLOR_MAPPING.items():
        kw = {"facecolor": color, "alpha": 0.5, "edgecolor": "black", "linewidth": 0.5}
        if label == 0:
            kw["hatch"] = "////"
            kw["label"] = "Not enough information"
        else:
            kw["label"] = ZONE_LABEL.get(label, str(label))
        patches.append(mpatches.Patch(**kw))
    plt.legend(
        handles=patches,
        title="Dengue Risk Levels",
        loc="upper left",
        bbox_to_anchor=(1.02, 1),
        borderaxespad=0,
    ).get_frame().set_edgecolor("black")

    ax.axis("off")
    plt.suptitle(f"{figure_title}\n({thisdate})", x=0.52, y=0.95)

    end_str = (
        pd.Timestamp(run_date).date().strftime("%Y%m%d")
        if run_date
        else pd.Timestamp.today().date().strftime("%Y%m%d")
    )
    fname = f"{REGION_LABEL.get(region, region)}s_{thisdate}_{MODEL_LABEL.get(model, model)}_{THRESHOLD_LABEL.get(threshold, threshold)}_{end_str}.png"
    out_path = os.path.join(output_dir, fname)
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    return out_path


def render_hero_forecast(
    predictions_df: pd.DataFrame,
    *,
    observed_df: "pd.DataFrame | None" = None,
    run_date: "pd.Timestamp | None" = None,
    out_path: str,
) -> str:
    """Rich hero forecast chart: observed + historical average + forecast with confidence band.

    Falls back gracefully if observed_df is None/empty (plots forecast only).
    If predictions_df is empty, renders a placeholder figure.
    """
    import numpy as np

    fig, ax = plt.subplots(figsize=(10, 5), dpi=140)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="y", color="#e0e0e0", linewidth=0.8, zorder=0)

    if predictions_df.empty:
        ax.text(
            0.5,
            0.5,
            "No forecast data available",
            transform=ax.transAxes,
            ha="center",
            va="center",
            fontsize=14,
            color="#888",
        )
        fig.savefig(out_path, bbox_inches="tight")
        plt.close(fig)
        return out_path

    # ── determine run_date ────────────────────────────────────────────────────
    if run_date is None:
        run_date = pd.Timestamp.today()

    # ── select ensemble model rows, single thresholdMethod ───────────────────
    ens_col = "ensembleModel"
    pred_col = "thresholdMethod"
    has_ensemble = (
        "model" in predictions_df.columns and predictions_df["model"].eq(ens_col).any()
    )
    fc_df = (
        predictions_df[predictions_df["model"] == ens_col].copy()
        if has_ensemble
        else predictions_df.copy()
    )

    if pred_col in fc_df.columns:
        if "historical" in fc_df[pred_col].values:
            fc_df = fc_df[fc_df[pred_col] == "historical"]
        else:
            fc_df = fc_df[fc_df[pred_col] == fc_df[pred_col].iloc[0]]

    fc_df["_week_start"] = pd.to_datetime(fc_df["startDatePredictedWeek"])
    has_std = "StdDev" in fc_df.columns
    agg_spec: dict = {"prediction": ("prediction", "sum")}
    if has_std:
        agg_spec["std_band"] = (
            "StdDev",
            lambda x: float(np.sqrt((x**2).sum())),
        )
    weekly_fc = (
        fc_df.groupby("_week_start")
        .agg(**agg_spec)
        .reset_index()
        .sort_values("_week_start")
    )
    if not has_std:
        weekly_fc["std_band"] = 0.0

    first_pred_date = weekly_fc["_week_start"].iloc[0]
    last_pred_date = weekly_fc["_week_start"].iloc[-1]

    # ── observed weekly cases ─────────────────────────────────────────────────
    has_observed = (
        observed_df is not None
        and not observed_df.empty
        and "date" in observed_df.columns
        and "case_count" in observed_df.columns
    )
    obs_legend_label = f"Observed ({run_date.year} YTD)"
    hist_years: list[int] = []
    ytd_total = 0

    if has_observed:
        obs = observed_df.copy()
        obs["date"] = pd.to_datetime(obs["date"])
        obs["_week_start"] = obs["date"] - pd.to_timedelta(
            obs["date"].dt.dayofweek, unit="D"
        )
        state_weekly = obs.groupby(["_week_start"])["case_count"].sum().reset_index()
        state_weekly["year"] = state_weekly["_week_start"].dt.year
        state_weekly["iso_week"] = (
            state_weekly["_week_start"].dt.isocalendar().week.astype(int)
        )

        current_year = run_date.year
        obs_current = state_weekly[
            (state_weekly["year"] == current_year)
            & (state_weekly["_week_start"] <= run_date)
        ].sort_values("_week_start")

        ytd_total = int(obs_current["case_count"].sum())
        obs_legend_label = f"Observed ({current_year} YTD)"

        # historical average per ISO week (years < current_year)
        hist_df = state_weekly[state_weekly["year"] < current_year]
        hist_years = sorted(hist_df["year"].unique().tolist())
        hist_avg = hist_df.groupby("iso_week")["case_count"].mean().reset_index()

        if not obs_current.empty:
            ax.plot(
                obs_current["_week_start"],
                obs_current["case_count"],
                color="#1f77b4",
                marker="o",
                linewidth=2,
                label=obs_legend_label,
                zorder=3,
            )
            for _, row in obs_current.iterrows():
                ax.annotate(
                    str(int(row["case_count"])),
                    xy=(row["_week_start"], row["case_count"]),
                    xytext=(0, 6),
                    textcoords="offset points",
                    ha="center",
                    fontsize=7,
                    color="#1f77b4",
                )

        # plot historical average mapped onto current year's week-start dates
        if not hist_avg.empty and not obs_current.empty:
            # build a mapping: iso_week → mean
            hist_map = dict(zip(hist_avg["iso_week"], hist_avg["case_count"]))
            hist_plot = obs_current.copy()
            hist_plot["hist_val"] = hist_plot["iso_week"].map(hist_map)
            hist_plot = hist_plot.dropna(subset=["hist_val"])
            if not hist_plot.empty:
                hist_label_years = (
                    f"{min(hist_years)}–{max(hist_years)}"
                    if len(hist_years) > 1
                    else str(hist_years[0])
                )
                ax.plot(
                    hist_plot["_week_start"],
                    hist_plot["hist_val"],
                    color="#888888",
                    linestyle=":",
                    linewidth=1.8,
                    label=f"Historical avg ({hist_label_years})",
                    zorder=2,
                )

    # ── forecast line + confidence band ──────────────────────────────────────
    ax.plot(
        weekly_fc["_week_start"],
        weekly_fc["prediction"],
        color="#d62728",
        marker="o",
        linewidth=2,
        label="Forecast",
        zorder=3,
    )
    ax.fill_between(
        weekly_fc["_week_start"],
        (weekly_fc["prediction"] - weekly_fc["std_band"]).clip(lower=0),
        weekly_fc["prediction"] + weekly_fc["std_band"],
        color="#d62728",
        alpha=0.15,
        zorder=1,
    )
    for _, row in weekly_fc.iterrows():
        ax.annotate(
            str(int(round(row["prediction"]))),
            xy=(row["_week_start"], row["prediction"]),
            xytext=(0, 6),
            textcoords="offset points",
            ha="center",
            fontsize=7,
            color="#d62728",
        )

    # ── vertical separator at run_date ───────────────────────────────────────
    ax.axvline(run_date, color="#999999", linestyle="--", linewidth=1.2, zorder=2)

    # ── annotations ──────────────────────────────────────────────────────────
    if has_observed and ytd_total:
        ax.text(
            0.01,
            0.97,
            f"{run_date.year} YTD total: {ytd_total:,} cases",
            transform=ax.transAxes,
            va="top",
            ha="left",
            fontsize=9,
            color="#1f77b4",
        )

    # forecast horizon callout — placed near top of forecast region
    fc_horizon_label = f"forecast horizon ({first_pred_date.strftime('%d %b')} – {last_pred_date.strftime('%d %b')})"
    ax.text(
        first_pred_date + (last_pred_date - first_pred_date) / 2,
        ax.get_ylim()[1] * 0.97,
        fc_horizon_label,
        ha="center",
        va="top",
        fontsize=8,
        color="#d62728",
        style="italic",
    )

    # ── labels / legend / formatting ─────────────────────────────────────────
    ax.set_xlabel("Week starting", fontsize=10)
    ax.set_ylabel("Cases", fontsize=10)
    ax.legend(fontsize=9, framealpha=0.9)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    return out_path

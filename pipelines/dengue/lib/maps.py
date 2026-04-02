"""Map generation functions (choropleth risk maps)."""

from __future__ import annotations

import math
import os
from pathlib import Path

import geopandas as gpd
import matplotlib

# Pipeline runner executes steps in worker threads; macOS GUI backend raises.
matplotlib.use("Agg")

import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import pandas as pd

COLOR_MAPPING = {1: "green", 2: "yellow", 3: "orange", 4: "red", 0: "w"}
ZONE_LABEL = {1: "Low", 2: "Low Medium", 3: "Medium", 4: "High"}

REGION_LABEL = {
    "corp": "Corp",
    "zone": "Zone",
    "ward": "Ward",
    "district": "District",
    "subdistrict": "Subdistrict",
}
MODEL_LABEL = {
    "negativeBinomialRegression": "Negative Binomial Regression",
    "ensembleModel": "Ensemble Model",
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
    geojson_folder = os.path.join(
        geojson_base, f"{REGION_LABEL.get(region, region).lower()}s"
    )
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    gdf_list = []
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
                gdf["color"] = COLOR_MAPPING.get(int(code), "w")
                gdf["hatch"] = ""
            else:
                gdf["color"] = "lightgray"
                gdf["hatch"] = ""
        else:
            gdf["color"] = "w"
            gdf["hatch"] = "////"
        gdf_list.append(gdf)

    if not gdf_list:
        return ""

    gdf_all = pd.concat(gdf_list, ignore_index=True)
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

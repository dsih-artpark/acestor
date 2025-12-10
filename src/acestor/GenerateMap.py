# -*- coding: utf-8 -*-
"""
Created on Sat Jun  8 22:43:03 2024

@author: TarunK
"""

import geopandas as gpd
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import os
import pickle
import logging

logger = logging.getLogger(__name__)


# with open("dumps/monthstring.pkl", "rb") as f:
# monthstring = pickle.load(f)

endString = pd.Timestamp.today().date().strftime(format="%Y%m%d")


# %%
def genPlot(
    color_df,
    geojson_folder,
    region_title,
    color_mapping,
    region="district",
    model="negativeBinomialRegression",
    threshold="historical",
    thisdate=None,
):
    """Generate the plots."""
    # Date in string format
    datestring = thisdate
    # Path to the folder containing the GeoJSON files

    # Create a GeoDataFrame to hold all district geometries
    gdf_list = []

    print(list(color_df["regionID"]))

    # Read each GeoJSON file and assign the corresponding color
    listJsons = os.listdir(geojson_folder)
    for file in os.listdir(geojson_folder):
        if file.endswith(".geojson"):
            region_gdf = gpd.read_file(os.path.join(geojson_folder, file))
            region_name = os.path.splitext(file)[0]  # Assume district name is the file name without extension

            # Find the corresponding color code from the CSV file
            try:
                color_code = color_df.loc[color_df["regionID"] == region_name, "predictionZone"].values[0]
                color = color_mapping[color_code]
                hatch = ""
                logger.info(f"Color code found for {region_name}")
            except Exception as e:
                logger.info(f"No color code found for {region_name}")
                color = color_mapping[0]
                hatch = "////"

            # Add the color as a new column in the GeoDataFrame
            region_gdf["color"] = color
            region_gdf["hatch"] = hatch

            # Append to the main GeoDataFrame
            gdf_list.append(region_gdf)

    gdf_all = pd.concat(gdf_list, ignore_index=True)
    gdf_all.loc[gdf_all["color"] == "w", "hatch"] = "////"

    # Dissolve boundaries between adjacent polygons
    gdf_dissolved = gdf_all.dissolve(by="regionName")

    # Plot the districts with the corresponding colors
    fig, ax = plt.subplots(1, 1, figsize=(8, 10), dpi=140)

    for color in color_mapping.values():
        thisgdf = gdf_all[gdf_all["color"] == color]
        if (len(thisgdf) != 0) and color != "w":
            thisgdf.plot(ax=ax, facecolor=color, edgecolor="none", alpha=0.5)
        elif (len(thisgdf) != 0) and color == "w":
            thisgdf.plot(ax=ax, facecolor=color, edgecolor="none", alpha=0.5, hatch="////")
        # elif (len(thisgdf) == 0) and (color=='w'):
        #     thisgdf.plot(ax=ax, color='w', edgecolor='black', alpha=0.5, linewidth=0.5)
        # else :
        #     thisgdf.plot(ax=ax, color='w', edgecolor='black', alpha=0.5, linewidth=0.5)

    # Plot the dissolved boundaries once
    gdf_dissolved.boundary.plot(ax=ax, edgecolor="black", linewidth=0.5, alpha=0.5)

    if region == "district":
        for idx, row in gdf_all.iterrows():
            # Calculate the centroid for each district
            centroid = row["geometry"].centroid
            # Annotate the map with the district name at the centroid location
            ax.annotate(
                row["regionName"].title(),
                xy=(centroid.x, centroid.y),
                xytext=(0, 0),
                textcoords="offset points",
                horizontalalignment="center",
                fontsize=6,
                color="black",
            )

    ax.set_aspect("equal")

    # Create legend patches
    legend_patches = []
    for label, color in color_mapping.items():
        if label != 0:
            legend_patches.append(mpatches.Patch(facecolor=color, alpha=0.5, edgecolor="black", linewidth=0.5, label=label))
        else:
            legend_patches.append(
                mpatches.Patch(facecolor=color, alpha=0.5, edgecolor="black", linewidth=0.5, label="Not enough information", hatch="////")
            )

    # Add legend to the plot
    plt.legend(handles=legend_patches, title="Dengue Risk Zones", loc="lower left", bbox_to_anchor=(0.8, 0.5)).get_frame().set_edgecolor(
        "black"
    )

    ax.axis("off")

    # Show the plot
    plt.title(f"{region_title} Dengue Risk Map\n({thisdate}) ({model} - {threshold})")
    endString = pd.Timestamp.today().date().strftime(format="%Y%m%d")
    plt.savefig(f"plots/{region_title}_{datestring}_{model}_{threshold}_{endString}.png")
    plt.show()
    plt.close()


def main():
    pass
    # global dictRegion, dictModel, dictThreshold, color_mapping
    # dictRegion = {"district": "District", "subdistrict": "Subdistrict", "state": "Karnataka"}

    # dictModel = {
    #     "negativeBinomialRegression": "Negative Binomial Regression",
    #     # 'timeSeriesExtrapolation': 'Time Series Extrapolation',
    #     "ensembleModel": "Ensemble Model",
    # }

    # dictThreshold = {"historical": "Historical Thresholds", "previousNweeks": "Previous N-Weeks Thresholds"}

    # # Define the color mapping
    # color_mapping = {1: "green", 2: "yellow", 3: "orange", 4: "red", 0: "w"}

    # listoutfiles = [val for val in os.listdir("results") if ((val.endswith(".csv")) and (monthstring in val) and (endString in val))]

    # for folder in dictRegion.keys():
    #     print(folder)
    #     # Path to the CSV file that contains the color codes
    #     if folder != "state":
    #         outfilename = [val for val in listoutfiles if f"{dictRegion[folder]}" in val][0]
    #         csv_file = f"results/{outfilename}"
    #         # Read the CSV file containing the color codes
    #         df = pd.read_csv(csv_file)
    #         for thisdate in df["startDatePredictedWeek"].unique():
    #             date_df = df[(df["startDatePredictedWeek"] == thisdate)]
    #             for model in dictModel.keys():
    #                 model_df = date_df[(date_df["model"] == model)]
    #                 if len(model_df) == 0:
    #                     continue
    #                 else:
    #                     for threshold in dictThreshold.keys():
    #                         color_df = model_df[model_df["thresholdMethod"] == threshold]
    #                         genPlot(color_df, geojson_folder, region=folder, model=model, threshold=threshold, thisdate=thisdate)
    #                         print(folder, thisdate, model, threshold)
    # color_df = deepcopy(model_df)
    # genPlot(color_df=color_df, region=folder, model=model, thisdate=thisdate)
    # print(folder, thisdate, model)


def generate_map(df, region_type, region_name, geojson_folder):
    # Define the color mapping
    color_mapping = {1: "green", 2: "yellow", 3: "orange", 4: "red", 0: "w"}
    for thisdate in df["startDatePredictedWeek"].unique():
        date_df = df[(df["startDatePredictedWeek"] == thisdate)]
        for model in date_df["model"].unique():
            model_df = date_df[(date_df["model"] == model)]
            if len(model_df) == 0:
                continue
            else:
                for threshold in model_df["thresholdMethod"].unique():
                    color_df = model_df[model_df["thresholdMethod"] == threshold]
                    genPlot(
                        color_df,
                        geojson_folder,
                        region_name,
                        color_mapping,
                        region=region_type,
                        model=model,
                        threshold=threshold,
                        thisdate=thisdate,
                    )
                    print(region_type, thisdate, model, threshold)

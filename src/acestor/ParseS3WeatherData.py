import xarray as xr
import pandas as pd
import geopandas as gpd
import logging
from shapely.ops import Point
from datetime import datetime, timedelta

from ParseAndExtractWeatherData import (
    get_region_gdfs,
    get_region_centroids,
    get_datafile_list,
    get_gridpoints,
    filter_far_points_km,
    estimate_values_at_centroids,
    deepcopy,
    rename_relevant_columns_and_save,
)

logger = logging.getLogger("parse-s3-weather-data")


def correct_timezones_and_aggregate_to_day(df):
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    df["time"] = df["time"] + timedelta(hours=5.5)
    df["date"] = df["time"].dt.date
    list_dates = list(df["date"].unique())
    list_dates = list_dates[1:-1]
    # Remove the first and the last dates because they contain partial information of a day because of IST correction
    df = df[df["date"].isin(list_dates)]
    df = df.sort_values(["region_id", "date"], ascending=[True, True])
    return df


def aggregate_n_days(all_centroid_data, common_cols, w_params, operations, N=7):
    def rolling_fn(group, common_cols=common_cols):
        group = group.sort_values("date").reset_index()  # Ensure data is sorted by date
        group = group.set_index("date")  # Set date as index for rolling operation
        # Perform rolling aggregation on numeric columns
        rolling_df = group[w_params].rolling(f"{N - 1}D", closed="both").agg({v1: v2 for v1, v2 in zip(w_params, operations)}).reset_index()
        print(rolling_df.head())
        # Add common columns to rolling_df
        rolling_df = rolling_df.merge(group.reset_index()[common_cols], on="date", how="left")
        return rolling_df

    all_centroid_data["date"] = pd.to_datetime(all_centroid_data["date"])

    # first aggregate daily
    mapped = {f"{col}_{func}": (col, func) for col, func in zip(w_params, operations)}
    agg_daily_df = all_centroid_data.groupby(common_cols, as_index=False).agg(**mapped)
    agg_daily_df = agg_daily_df.sort_values(["region_id", "date"], ascending=[True, True]).reset_index(drop=True)

    w_params = ["t2m_max", "t2m_min", "t2m_mean", "d2m_mean", "tp_sum"]
    operations = ["mean", "mean", "mean", "mean", "sum"]
    listcols = common_cols + w_params
    df_aggN = agg_daily_df.groupby("region_id")[listcols].apply(rolling_fn).reset_index(drop=True)
    df_aggN = df_aggN[listcols]
    return df_aggN


def parse_s3_weather_data(weather_data_path, geojson_folder_path, start_date, end_date, sampling_day):
    w_params = ["t2m", "d2m", "tp"]
    region_gdf_list = get_region_gdfs(geojson_folder=geojson_folder_path, region_type="district", target_crs="EPSG:4326")
    region_centroid_list = [get_region_centroids(gdf=gdf)["geometry"][0] for gdf in region_gdf_list]
    list_data_files = get_datafile_list(source_path=weather_data_path, filetypes=[".nc", ".zip"])

    bbox = ((24, 17), (80, 85))

    df_list = []

    for file in list_data_files:
        src_file = file
        dest_file = str(src_file).replace(".nc", ".csv")

        # print(magic.from_file(file))

        ds = xr.open_dataset(file, engine="netcdf4")
        df = ds.sel(latitude=slice(*bbox[0]), longitude=slice(*bbox[1])).compute().to_dataframe().reset_index()
        start = pd.to_datetime(start_date)
        end = pd.to_datetime(end_date)
        now = pd.Timestamp.now()
        ninety_days_ago = now - pd.Timedelta(days=90)
        print("---weather-parse-dates---")
        print(start)
        print(end)

        time_condition = ((df["valid_time"] > start) & (df["valid_time"] < end)) | (
            (df["valid_time"] > ninety_days_ago) & (df["valid_time"] <= now)
        )

        df = df[time_condition]

        df = df.rename(columns={"valid_time": "time"})
        df = df.sort_values(by=["time", "longitude", "latitude"], ascending=[True, True, True])
        df = df.drop(columns=["number", "expver"])
        df = df.set_index(["time", "latitude", "longitude"])
        # if df.shape[0] > 0:
        df_list.append(df)

    df = pd.concat(df_list, axis=1).reset_index()
    df_na = df[df.isnull().any(axis=1)]
    # df_na = df[df.isna()]
    df = df.dropna()

    # na dates
    logger.info(f"NA's found & dropped on dates: {df_na['time'].unique()}")

    df.to_csv("weather_data_test.csv", index=False)
    gdf_data = gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df.longitude, df.latitude), crs="EPSG:4326")
    # Extract grid coordinates from the raw data
    gdf_points = get_gridpoints(df=df)
    # Filter out far away points for every region
    filtered_regions = [
        filter_far_points_km(gdf_points=gdf_points, gdf_map=gdf, threshold_km=25).reset_index(drop=True) for gdf in region_gdf_list
    ]
    # Begin - These steps ensures that the decimal point conversion issues resulting from previous step are addressed
    for i in range(len(filtered_regions)):
        gdf = deepcopy(filtered_regions[i])
        gdf.loc[:, "geometry"] = gdf["geometry"].apply(lambda p: Point(round(p.x, 2), round(p.y, 2)))
        filtered_regions[i] = gdf
    # filtered_regions = [get_gridpoints(gdf) for gdf in filtered_regions]
    # End - These steps ensures that the decimal point conversion issues resulting from previous step are addressed

    # Extract region specific parameters
    region_data_list = [
        gpd.sjoin(gdf_data, gdf, on_attribute=["latitude", "longitude"], how="inner", predicate="intersects")
        .reset_index(drop=True)
        .drop(columns=["index_right"])
        for gdf in filtered_regions
    ]
    # Estimate region centroid specific parameters

    centroid_values = []
    for v1, v2, v3 in zip(region_data_list, region_centroid_list, region_gdf_list):
        df_centroid = estimate_values_at_centroids(target_vars=w_params, region_data=v1, region_centroid=v2)
        df_centroid.loc[:, "region_id"] = v3.loc[0, "region_id"]
        df_centroid.loc[:, "name"] = v3.loc[0, "name"]
        df_centroid.loc[:, "parent"] = v3.loc[0, "parent"]
        df_centroid.loc[:, "parent_name"] = v3.loc[0, "parent_name"]
        centroid_values.append(df_centroid)

    # Concatenate all region centroid specific parameters
    all_centroid_data = pd.concat(centroid_values).reset_index(drop=True)
    all_centroid_data.to_csv("all_centroid_data_test.csv", index=False)

    # Daily data aggregation: Example usage
    w_params = ["t2m", "t2m", "t2m", "d2m", "tp"]
    operations = ["max", "min", "mean", "mean", "sum"]
    common_cols = ["region_id", "date", "name", "parent", "parent_name"]

    all_centroid_data = correct_timezones_and_aggregate_to_day(all_centroid_data)
    all_centroid_data.to_csv("timezone_corrected.csv", index=False)

    aggregated_data = aggregate_n_days(
        all_centroid_data,
        common_cols=common_cols,
        w_params=w_params,
        operations=operations,
    )

    aggregated_data.to_csv("agg_data_test.csv")

    # sample data

    day_abbr = sampling_day.split("-")[-1]  # Get 'WED' from 'W-WED'
    print(day_abbr)
    # Filter to only dates matching this day of week
    df_sample = aggregated_data[aggregated_data["date"].dt.strftime("%a").str.upper() == day_abbr].reset_index(drop=True)

    df_sample.to_csv("datasets/weather_district_sampled.csv", index=False)

    listcols = ["region_id", "date", "name", "t2m_mean", "d2m_mean", "tp_sum"]
    col_names = {
        "date": "metadata.primaryDate",
        "t2m_mean": "2mTemperature",
        "d2m_mean": "2mDewpointTemperature",
        "tp_sum": "totalPrecipitation",
    }

    col_names["region_id"] = "location.admin2.ID"
    col_names["name"] = "location.admin2.name"

    rename_relevant_columns_and_save(
        input_path="datasets/weather_district_sampled.csv",
        listcols=listcols,
        col_names=col_names,
        output_path="datasets/processed_aggregated_era5_Karnataka_District.csv",
    )

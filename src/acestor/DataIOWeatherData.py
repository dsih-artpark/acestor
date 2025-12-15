"""
DataIO Weather Data Module

This module provides functions for:
1. Downloading weather data using the DataIO API
2. Parsing weather data from NetCDF to CSV format
"""

import logging
from datetime import timedelta
from typing import Dict, List, Optional, Union

import geopandas as gpd
import pandas as pd
import xarray as xr
from dataio import DataIOAPI

from ParseAndExtractWeatherData import (
    deepcopy,
    estimate_values_at_centroids,
    rename_relevant_columns_and_save,
    filter_far_points_km,
    get_gridpoints,
    get_region_centroids,
    get_region_gdfs,
)

logger = logging.getLogger("dataio-weather-data")


def initialize_dataio_client(
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    data_dir: Optional[str] = "data",
) -> DataIOAPI:
    """
    Initialize DataIO API client.

    Args:
        base_url: Base URL for DataIO API. If None, reads from DATAIO_API_BASE_URL env var
        api_key: API key for authentication. If None, reads from DATAIO_API_KEY env var
        data_dir: Directory to store downloaded data. Defaults to 'data'

    Returns:
        DataIOAPI: Initialized DataIO client

    Example:
        # Using environment variables
        client = initialize_dataio_client()

        # Using direct credentials
        client = initialize_dataio_client(
            base_url="https://dataio.artpark.ai/api/v1",
            api_key="your_api_key_here"
        )
    """
    if base_url and api_key:
        logger.info("Initializing DataIO client with provided credentials")
        client = DataIOAPI(base_url=base_url, api_key=api_key, data_dir=data_dir)
    else:
        logger.info("Initializing DataIO client from environment variables")
        client = DataIOAPI()

    return client


def list_available_weather_datasets(client: DataIOAPI) -> pd.DataFrame:
    """
    List all available weather datasets from DataIO.

    Args:
        client: Initialized DataIO API client

    Returns:
        pd.DataFrame: DataFrame containing dataset information including
                     temporal/spatial coverage and available variables

    Example:
        client = initialize_dataio_client()
        datasets = list_available_weather_datasets(client)
        print(datasets)
    """
    logger.info("Fetching available weather datasets")
    datasets = client.list_weather_datasets()
    return datasets


def download_weather_data(
    client: DataIOAPI,
    dataset_name: str,
    variables: List[str],
    start_date: str,
    end_date: str,
    geojson: Union[str, Dict],
    output_dir: Optional[str] = None,
) -> xr.Dataset:
    """
    Download weather data from DataIO API.

    Args:
        client: Initialized DataIO API client
        dataset_name: Name of the dataset (e.g., "era5_sfc")
        variables: List of climate variables to download (e.g., ["t2m", "tp", "d2m"])
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format
        geojson: Region specification - either state ID (e.g., "state_29") or GeoJSON geometry
        output_dir: Optional custom directory for saving data

    Returns:
        xr.Dataset: Downloaded weather data as xarray Dataset

    Example:
        client = initialize_dataio_client()
        data = download_weather_data(
            client=client,
            dataset_name="era5_sfc",
            variables=["t2m", "tp", "d2m"],
            start_date="2024-01-01",
            end_date="2024-01-31",
            geojson="state_29"
        )
    """
    logger.info(f"Downloading weather data for {dataset_name}")
    logger.info(f"Date range: {start_date} to {end_date}")
    logger.info(f"Variables: {variables}")

    dataset = client.download_weather_data(
        dataset_name=dataset_name,
        variables=variables,
        start_date=start_date,
        end_date=end_date,
        geojson=geojson,
        output_dir=output_dir,
    )

    logger.info(f"Successfully downloaded weather data with shape: {dataset.dims}")
    return dataset


def download_weather_data_batch(
    client: DataIOAPI,
    dataset_name: str,
    variables: List[str],
    start_date: str,
    end_date: str,
    regions: List[Union[str, Dict]],
    output_dir: Optional[str] = None,
) -> Dict[str, xr.Dataset]:
    """
    Download weather data for multiple regions in batch.

    Args:
        client: Initialized DataIO API client
        dataset_name: Name of the dataset
        variables: List of climate variables to download
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format
        regions: List of region specifications (state IDs or GeoJSON geometries)
        output_dir: Optional custom directory for saving data

    Returns:
        Dict[str, xr.Dataset]: Dictionary mapping region identifier to downloaded dataset

    Example:
        client = initialize_dataio_client()
        regions = ["state_29", "state_28", "state_27"]
        datasets = download_weather_data_batch(
            client=client,
            dataset_name="era5_sfc",
            variables=["t2m", "tp"],
            start_date="2024-01-01",
            end_date="2024-01-31",
            regions=regions
        )
    """
    logger.info(f"Batch downloading weather data for {len(regions)} regions")
    datasets = {}

    for region in regions:
        region_id = region if isinstance(region, str) else "custom_region"
        logger.info(f"Downloading data for region: {region_id}")

        try:
            dataset = download_weather_data(
                client=client,
                dataset_name=dataset_name,
                variables=variables,
                start_date=start_date,
                end_date=end_date,
                geojson=region,
                output_dir=output_dir,
            )
            datasets[region_id] = dataset
        except Exception as e:
            logger.error(f"Failed to download data for region {region_id}: {e!r}")
            continue

    logger.info(f"Successfully downloaded data for {len(datasets)} regions")
    return datasets


def netcdf_to_csv(
    netcdf_path: str,
    output_csv_path: str,
    bbox: Optional[tuple] = None,
    time_range: Optional[tuple] = None,
    drop_columns: Optional[List[str]] = None,
) -> pd.DataFrame:
    """
    Convert NetCDF weather data file to CSV format.

    Args:
        netcdf_path: Path to input NetCDF file
        output_csv_path: Path for output CSV file
        bbox: Optional bounding box as ((lat_min, lat_max), (lon_min, lon_max))
        time_range: Optional time range as (start_date, end_date) strings
        drop_columns: Optional list of columns to drop

    Returns:
        pd.DataFrame: Converted weather data as DataFrame

    Example:
        df = netcdf_to_csv(
            netcdf_path="data/weather.nc",
            output_csv_path="data/weather.csv",
            bbox=((17, 24), (80, 85)),
            time_range=("2024-01-01", "2024-01-31"),
            drop_columns=["number", "expver"]
        )
    """
    logger.info(f"Converting NetCDF to CSV: {netcdf_path}")

    # Open NetCDF dataset
    ds = xr.open_dataset(netcdf_path, engine="netcdf4")

    # Apply spatial filtering if bbox is provided
    if bbox:
        lat_slice, lon_slice = bbox
        ds = ds.sel(latitude=slice(*lat_slice), longitude=slice(*lon_slice))

    # Convert to dataframe
    df = ds.compute().to_dataframe().reset_index()

    # Apply time filtering if provided
    if time_range:
        start_date, end_date = time_range
        start = pd.to_datetime(start_date)
        end = pd.to_datetime(end_date)

        # Check for time column (could be 'time' or 'valid_time')
        time_col = "valid_time" if "valid_time" in df.columns else "time"
        df = df[(df[time_col] >= start) & (df[time_col] <= end)]

        # Rename to consistent 'time' column
        if time_col == "valid_time":
            df = df.rename(columns={"valid_time": "time"})

    # Drop specified columns
    if drop_columns:
        df = df.drop(columns=[col for col in drop_columns if col in df.columns])

    # Drop NaN values
    df = df.dropna()

    # Save to CSV
    df.to_csv(output_csv_path, index=False)
    logger.info(f"Successfully saved CSV to: {output_csv_path}")
    logger.info(f"DataFrame shape: {df.shape}")

    return df


def parse_dataio_weather_to_csv(
    root_dir,
    dataset: xr.Dataset,
    output_csv_path: str,
    geojson_folder_path: str,
    start_date: str,
    end_date: str,
    sampling_day: str,
    bbox: tuple = ((17, 24), (80, 85)),
    region_type: str = "district",
    config=None,
) -> pd.DataFrame:
    """
    Parse DataIO weather dataset and save aggregated data to CSV.

    This function performs the full pipeline:
    1. Converts xarray Dataset to DataFrame
    2. Filters by bounding box and time range
    3. Matches weather data to region centroids
    4. Aggregates to daily values
    5. Creates rolling averages
    6. Samples data by specified day of week

    Args:
        dataset: xarray Dataset from DataIO download
        output_csv_path: Path for final output CSV file
        geojson_folder_path: Path to folder containing region GeoJSON files
        start_date: Start date for filtering (YYYY-MM-DD)
        end_date: End date for filtering (YYYY-MM-DD)
        sampling_day: Day of week for sampling (e.g., "W-WED")
        bbox: Bounding box as ((lat_min, lat_max), (lon_min, lon_max))
        region_type: Type of regions (e.g., "district", "state")

    Returns:
        pd.DataFrame: Processed and aggregated weather data

    Example:
        client = initialize_dataio_client()
        dataset = download_weather_data(...)
        df = parse_dataio_weather_to_csv(
            dataset=dataset,
            output_csv_path="datasets/weather_processed.csv",
            geojson_folder_path="geojsons",
            start_date="2024-01-01",
            end_date="2024-01-31",
            sampling_day="W-WED"
        )
    """
    logger.info("Parsing DataIO weather dataset to CSV")

    # Get region information
    w_params = ["t2m", "d2m", "tp"]
    region_gdf_list = get_region_gdfs(geojson_folder=geojson_folder_path, region_type=region_type, target_crs="EPSG:4326")
    region_centroid_list = [get_region_centroids(gdf=gdf)["geometry"][0] for gdf in region_gdf_list]

    lat_descending = False
    if "latitude" in dataset.coords:
        lat_values = dataset.latitude.values
        lat_descending = lat_values[0] > lat_values[-1]

    # xarray slice order must match coordinate order in dataset
    # If latitude is descending, reverse the slice (max, min instead of min, max)
    lat_slice = slice(*bbox[0]) if not lat_descending else slice(bbox[0][1], bbox[0][0])
    lon_slice = slice(*bbox[1])

    # Convert xarray Dataset to DataFrame
    dataset_filtered = dataset.sel(latitude=lat_slice, longitude=lon_slice)

    df = dataset_filtered.compute().to_dataframe().reset_index()

    # Filter by time range
    start = pd.to_datetime(start_date)
    end = pd.to_datetime(end_date)

    # Handle different time column names
    time_col = "valid_time" if "valid_time" in df.columns else "time"
    df = df[(df[time_col] >= start) & (df[time_col] <= end)]

    if time_col == "valid_time":
        df = df.rename(columns={"valid_time": "time"})

    # Sort and clean data
    df = df.sort_values(by=["time", "longitude", "latitude"], ascending=[True, True, True])

    # Drop unnecessary columns if they exist
    drop_cols = ["number", "expver"]
    df = df.drop(columns=[col for col in drop_cols if col in df.columns])

    # Drop NaN values
    df_na = df[df.isnull().any(axis=1)]
    df = df.dropna()
    logger.info(f"Dropped NAs found on dates: {df_na['time'].unique() if len(df_na) > 0 else 'None'}")

    # Create GeoDataFrame
    gdf_data = gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df.longitude, df.latitude), crs="EPSG:4326")

    # Extract grid coordinates
    gdf_points = get_gridpoints(df=df)

    logger.info(f"gdf_points columns: {gdf_points.columns.tolist()}")
    logger.info(f"gdf_points shape: {gdf_points.shape}")
    logger.info(f"Number of regions: {len(region_gdf_list)}")

    # Filter far points for each region
    filtered_regions = []
    for i, gdf in enumerate(region_gdf_list):
        # logger.info(f"Processing region {i + 1}/{len(region_gdf_list)}: {gdf.loc[0, 'name'] if 'name' in gdf.columns else 'Unknown'}")
        # logger.info(f"Region GDF columns: {gdf.columns.tolist()}")
        # logger.info(f"Region GDF shape: {gdf.shape}")
        filtered = filter_far_points_km(gdf_points=gdf_points, gdf_map=gdf, threshold_km=25).reset_index(drop=True)
        filtered_regions.append(filtered)

    # Round coordinates to avoid decimal conversion issues
    for i in range(len(filtered_regions)):
        gdf = deepcopy(filtered_regions[i])
        from shapely.ops import Point

        gdf.loc[:, "geometry"] = gdf["geometry"].apply(lambda p: Point(round(p.x, 2), round(p.y, 2)))
        filtered_regions[i] = gdf

    # Extract region-specific parameters
    region_data_list = [
        gpd.sjoin(gdf_data, gdf, on_attribute=["latitude", "longitude"], how="inner", predicate="intersects")
        .reset_index(drop=True)
        .drop(columns=["index_right"])
        for gdf in filtered_regions
    ]

    # Estimate centroid values
    centroid_values = []
    for region_data, region_centroid, region_gdf in zip(region_data_list, region_centroid_list, region_gdf_list, strict=True):
        df_centroid = estimate_values_at_centroids(target_vars=w_params, region_data=region_data, region_centroid=region_centroid)
        df_centroid.loc[:, "region_id"] = region_gdf.loc[0, "id"]
        df_centroid.loc[:, "name"] = region_gdf.loc[0, "regionName"]
        df_centroid.loc[:, "parent"] = region_gdf.loc[0, "parentID"]
        df_centroid.loc[:, "parent_name"] = region_gdf.loc[0, "parentName"]
        centroid_values.append(df_centroid)

    # Concatenate all centroid data
    all_centroid_data = pd.concat(centroid_values).reset_index(drop=True)

    # Correct timezones and aggregate to daily
    all_centroid_data = _correct_timezones_and_aggregate_to_day(all_centroid_data)

    # Aggregate to N-day rolling averages
    w_params = ["t2m", "t2m", "t2m", "d2m", "tp"]
    operations = ["max", "min", "mean", "mean", "sum"]
    common_cols = ["region_id", "date", "name", "parent", "parent_name"]

    aggregated_data = _aggregate_n_days(all_centroid_data, common_cols=common_cols, w_params=w_params, operations=operations)

    # Sample by specified day of week
    day_abbr = sampling_day.split("-")[-1]
    df_sample = aggregated_data[aggregated_data["date"].dt.strftime("%a").str.upper() == day_abbr].reset_index(drop=True)

    # Save final output
    df_sample.to_csv(output_csv_path, index=False)
    logger.info(f"Successfully saved processed data to: {output_csv_path}")

    listcols = ["region_id", "date", "name", "t2m_mean", "d2m_mean", "tp_sum"]
    col_names = {
        "date": "metadata.primaryDate",
        "t2m_mean": "2mTemperature",
        "d2m_mean": "2mDewpointTemperature",
        "tp_sum": "totalPrecipitation",
    }

    rename_relevant_columns_and_save(
        input_path=root_dir / "datasets/weather_district_sampled.csv",
        listcols=listcols,
        col_names=col_names,
        output_path=root_dir
        / f"datasets/processed_aggregated_era5_{config['region_name'].capitalize()}_{config['granularity'].capitalize()}.csv",
    )

    return df_sample


def _correct_timezones_and_aggregate_to_day(df: pd.DataFrame) -> pd.DataFrame:
    """Helper function to correct timezones and aggregate to daily values."""
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    df["time"] = df["time"] + timedelta(hours=5.5)  # Convert to IST
    df["date"] = df["time"].dt.date
    list_dates = list(df["date"].unique())
    list_dates = list_dates[1:-1]  # Remove first and last dates (partial data)
    df = df[df["date"].isin(list_dates)]
    df = df.sort_values(["region_id", "date"], ascending=[True, True])
    return df


def _aggregate_n_days(
    all_centroid_data: pd.DataFrame, common_cols: List[str], w_params: List[str], operations: List[str], N: int = 7
) -> pd.DataFrame:
    """Helper function to aggregate data to N-day rolling averages."""

    def rolling_fn(group, common_cols=common_cols):
        group = group.sort_values("date").reset_index()
        group = group.set_index("date")
        rolling_df = (
            group[w_params]
            .rolling(f"{N - 1}D", closed="both")
            .agg({v1: v2 for v1, v2 in zip(w_params, operations, strict=True)})
            .reset_index()
        )
        rolling_df = rolling_df.merge(group.reset_index()[common_cols], on="date", how="left")
        return rolling_df

    all_centroid_data["date"] = pd.to_datetime(all_centroid_data["date"])

    # First aggregate daily
    mapped = {f"{col}_{func}": (col, func) for col, func in zip(w_params, operations, strict=True)}
    agg_daily_df = all_centroid_data.groupby(common_cols, as_index=False).agg(**mapped)
    agg_daily_df = agg_daily_df.sort_values(["region_id", "date"], ascending=[True, True]).reset_index(drop=True)

    # log agg_daily_df columns  & shape
    logger.info(f"agg_daily_df columns: {agg_daily_df.columns.tolist()}")
    logger.info(f"agg_daily_df shape: {agg_daily_df.shape}")

    # Then apply rolling aggregation
    w_params = ["t2m_max", "t2m_min", "t2m_mean", "d2m_mean", "tp_sum"]
    listcols = common_cols + w_params
    df_aggN = agg_daily_df.groupby("region_id")[listcols].apply(rolling_fn).reset_index(drop=True)
    df_aggN = df_aggN[listcols]

    return df_aggN

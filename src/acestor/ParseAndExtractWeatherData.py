# -*- coding: utf-8 -*-
"""
Created on Mon Sep 30 16:09:57 2024.

@author: TarunK
"""

import numpy as np
import geopandas as gpd
import pandas as pd
from shapely.ops import Point
import xarray as xr
from copy import deepcopy
from pathlib import Path
import os
import logging
import zipfile
import magic
import datetime as dt
from sklearn.linear_model import LinearRegression
from typing import Literal

# Creating an object
logger = logging.getLogger("parse_and_extract_weather_data")


# %% Functions related to geo files: filtering points, plotting maps, plotting grid points, etc.
def read_geojson(*, filepath, target_crs="EPSG:4326"):
    """
    Read a geojson file, and return the geopandas dataframe and the union polygon.

    Parameters
    ----------
    filepath : str or Path
        The path to a geojson file.
    target_crs : str
        The desired crs in which we want to format the gopandas dataframe

    Returns
    -------
    gdf: geopandas dataframe
        The geopandas dataframe corresponding to the geojson file
    union_polygon: shapely.geometry.polygon.Polygon
        The union of all polygons in a geopandas dataframe

    """
    gdf = gpd.read_file(filepath)
    union_polygon = gdf.geometry.union_all()
    # union_polygon = unary_union(gdf.geometry)
    gdf = gdf.to_crs(target_crs)
    return gdf, union_polygon


def return_list_gdfs(*, geojson_folder):
    """
    Read geojson files in a given folder.

    Compute and return the list of geodataframe of all the polygons within the given folder.

    Parameters
    ----------
    geojson_folder : str or Path
        The path to the folder containing all the geojson files of interest.

    Returns
    -------
    gdfs : list (of geodataframe)
        list of geodataframes for all the polygons within the given folder.

    """
    geojson_folder = Path(geojson_folder)
    listfiles = os.listdir(geojson_folder)
    gdfs = [gpd.read_file(geojson_folder / thisfile).to_crs(epsg=4326) for thisfile in listfiles]
    return gdfs


def return_multiple_polygons_composite_gdf(*, geojson_folder):
    """
    Read geojson files in a given folder.

    Compute and return the composite geodataframe of all the polygons within the given folder.

    Parameters
    ----------
    geojson_folder : str or Path
        The path to the folder containing all the geojson files of interest.

    Returns
    -------
    region_gdf : geodataframe
        The composite geodataframe of all the polygons within the given folder.

    """
    geojson_folder = Path(geojson_folder)
    listfiles = os.listdir(geojson_folder)
    gdfs = [gpd.read_file(geojson_folder / thisfile) for thisfile in listfiles]
    region_gdf = gpd.GeoDataFrame(pd.concat(gdfs, ignore_index=True))
    region_gdf.to_crs(epsg=4326)
    return region_gdf


def generate_gridpoints(*, region_bounds, filepath):
    """
    Generate the grid points using the region_bounds and save as a geojson file.

    Parameters
    ----------
    region_bounds : tuple
        A tuple (of floats) containing bound_n, bound_w, bound_s, bound_e.
    filepath : string or Path
        The path to which we want to save the geojson file.

    Returns
    -------
    None.

    """
    rb = region_bounds
    bound_n, bound_w, bound_s, bound_e = rb[0], rb[1], rb[2], rb[3]

    setlat = np.linspace(bound_s, bound_n, num=int((bound_n - bound_s) / 0.25))
    setlon = np.linspace(bound_w, bound_e, num=int((bound_e - bound_w) / 0.25))
    # # Modified on 2025-01-06. Original is as follows
    # setlat = np.linspace(bound_s, bound_n + 0.25, num=int((bound_n - bound_s) / .25 + 1))
    # setlon = np.linspace(bound_w, bound_e + 0.25, num=int((bound_e - bound_w) / .25 + 1))

    df_latlon = pd.DataFrame()  # create a dataframe of the grid points (for which we have weather data)
    df_latlon.loc[:, "latitude"] = np.tile(setlat, len(setlon))
    df_latlon.loc[:, "longitude"] = np.repeat(setlon, len(setlat))

    gdf = gpd.GeoDataFrame(df_latlon, geometry=gpd.points_from_xy(df_latlon.longitude, df_latlon.latitude), crs="EPSG:4326")

    gdf.to_file(filepath, driver="GeoJSON")
    return None


def get_gridpoints(*, df, filepath="", save_file=False):
    """
    Get the grid points and save as a geojson file.

    Parameters
    ----------
    df : dataframe
        The dataframe parsed from a .nc (or a .zip) file.
        Contains the coordinates for which we have recorded data
    filepath : string or Path
        The path to which we want to save the geojson file.
    save_file : bool
        Specify the choice True (Yes) or False (No) to save the geojson file.

    Returns
    -------
    gdf: geopandas dataframe
        The geoPandas dataframe of the grid points.

    """
    setlat = np.sort(df["latitude"].unique())
    setlon = np.sort(df["longitude"].unique())

    df_latlon = pd.DataFrame()  # create a dataframe of the grid points (for which we have weather data)
    df_latlon.loc[:, "latitude"] = np.tile(setlat, len(setlon))
    df_latlon.loc[:, "longitude"] = np.repeat(setlon, len(setlat))

    gdf = gpd.GeoDataFrame(df_latlon, geometry=gpd.points_from_xy(df_latlon.longitude, df_latlon.latitude), crs="EPSG:4326")
    if save_file:
        gdf.to_file(filepath, driver="GeoJSON")
    return gdf


def filter_far_points_km(*, gdf_points, gdf_map, threshold_km=50):
    """
    Filter out points that are farther than the given distance threshold from the region boundary.

    Given:
    1. Polygon (map).
    2. Set of points for a potentially larger region for which weather data is available.

    Larger Goal: Estimate the weather parameters that represent the polygon.
    Goal: Identify which points from (2.) are reasonably close to or lie within the polygon (1.).
    Objective: We do not need all points from (2.) to estimate the weather parameters.

    Parameters
    ----------
    gdf_points : geopandas.GeoDataFrame
        The points for which weather data is available.
    gdf_map : geopandas.GeoDataFrame
        The area of interest.
    threshold_km : float, optional
        Distance threshold in kilometers to filter out points far from a polygon. Default is 50 km.

    Returns
    -------
    filtered_gdf_points : geopandas.GeoDataFrame
        Points that are within the distance threshold from a polygon.
    """
    # Ensure both GeoDataFrames have the same CRS
    if gdf_map.crs != gdf_points.crs:
        logging.info("CRS of points and map do not match! Check it!")
        gdf_points = gdf_points.to_crs(gdf_map.crs)

    # Reproject to a CRS that supports metric units (e.g., UTM)
    metric_crs = gdf_map.to_crs(epsg=3395)  # World Mercator (or choose appropriate CRS for your area)
    gdf_map_metric = gdf_map.to_crs(metric_crs.crs)
    gdf_points_metric = gdf_points.to_crs(metric_crs.crs)

    # Calculate distances in meters and filter points
    gdf_points_metric["distance_to_polygon"] = gdf_points_metric["geometry"].apply(
        lambda point: gdf_map_metric["geometry"].apply(lambda region: point.distance(region)).min()
    )

    # Convert threshold from kilometers to meters
    threshold_m = threshold_km * 1000
    # Filter points with distance less than or equal to the threshold
    filtered_gdf_points = gdf_points_metric[gdf_points_metric["distance_to_polygon"] <= threshold_m]
    # Drop the distance column if not needed anymore
    filtered_gdf_points = filtered_gdf_points.drop(columns=["distance_to_polygon"])
    # Reproject back to the original CRS if needed
    filtered_gdf_points = filtered_gdf_points.to_crs(gdf_points.crs)
    # Add the polygon name to the points
    filtered_gdf_points["name"] = gdf_map.loc[0, "regionName"]
    return filtered_gdf_points


def filter_far_points_with_degree_threshold(*, gdf_map, gdf_points, threshold_degree=0.5):
    """
    Filter out points that are farther than the given degree-based threshold from the region boundary.

    Parameters
    ----------
    gdf_map : geopandas.GeoDataFrame
        The area of interest (polygon in degrees).
    gdf_points : geopandas.GeoDataFrame
        The points for which weather data is available (in degrees).
    threshold_degree : float, optional
        Distance threshold in degrees to filter out points far from a polygon. Default is 0.5 degrees.

    Returns
    -------
    filtered_gdf_points : geopandas.GeoDataFrame
        Points that are within the degree threshold from a polygon.
    """
    # Ensure both GeoDataFrames have the same CRS
    if gdf_map.crs != gdf_points.crs:
        gdf_points = gdf_points.to_crs(gdf_map.crs)

    # Calculate distances in degrees and filter points
    gdf_points["distance_to_polygon"] = gdf_points.geometry.apply(
        lambda point: gdf_map.geometry.apply(lambda region: point.distance(region)).min()
    )

    # Filter points with distance less than or equal to the threshold
    filtered_gdf_points = gdf_points[gdf_points["distance_to_polygon"] <= threshold_degree]

    # Drop the distance column if not needed anymore
    filtered_gdf_points = filtered_gdf_points.drop(columns=["distance_to_polygon"])

    # Add the polygon name to the points
    filtered_gdf_points["name"] = gdf_map.loc[0, "name"]

    return filtered_gdf_points


def get_region_centroids(*, gdf):
    """
    Return GeoDataFrame containing the centroids of each polygon.

    There can be multiple regions (geometries) within the input GeoDataFrame.
    The function reprojects geometries to a projected CRS for accurate centroid calculations.

    Parameters
    ----------
    gdf : geopandas dataframe
        geopandas dataframe containing district geometries.

    Returns
    -------
    centroids : geopandas dataframe
        GeoDataFrame with centroids of the input geometries.
    """
    gdfcrs = gdf.crs
    # Reproject to a projected CRS (e.g., EPSG:3857) if necessary
    if gdf.crs.is_geographic:
        gdf = gdf.to_crs(epsg=3395)  # gdf = gdf.to_crs(epsg=3857)
    # Calculate centroids for each geometry
    centroids = gdf.copy()
    centroids["geometry"] = gdf.geometry.centroid
    # Reproject back to the original CRS
    centroids = centroids.to_crs(gdfcrs)
    return centroids


def get_region_gdfs(*, geojson_folder, region_type="district", target_crs="EPSG:4326"):
    """
    Fetch the list of geopandas dataframes of a specific region type present in a folder of geojsons.

    Parameters
    ----------
    geojson_folder : str or Path
        The folder containing geojsons.
    region_type : str, optional
        The type of region: district, subdistrict, etc. The default is 'district'.
    target_crs : str, optional
        The crs format in which we want the geopandas dataframes. The default is 'EPSG:4326'.

    Returns
    -------
    None.

    """
    # Iterate through all files in the folder and its subfolders
    list_gdf = []
    for root, _, files in os.walk(geojson_folder / f"{region_type}s"):
        for filename in files:
            # Check if the file name starts with the file_name_string
            if filename.startswith(region_type):
                file_path = os.path.join(root, filename)
                list_gdf.append(read_geojson(filepath=file_path, target_crs=target_crs)[0])
    return list_gdf


def get_datafile_list(*, source_path, filetypes):
    """
    Fetch the list of data files present in the source folder based on specified file types.

    Parameters
    ----------
    source_path : str or Path
        The folder containing data files.
    filetypes : list of str
        A list of file extensions to filter the files (e.g., ['.nc', '.zip', '.csv']).

    Returns
    -------
    datafile_list : list of Path
        The list of data files in source path matching the specified file types.
    """
    if not isinstance(filetypes, (list, tuple)) or not all(isinstance(ft, str) for ft in filetypes):
        raise ValueError("Filetypes must be a list or tuple of strings representing file extensions!")

    datafile_list = []
    for root, _, files in os.walk(source_path):
        for filename in files:
            if any(filename.endswith(ext) for ext in filetypes):
                file_path = Path(root) / filename
                datafile_list.append(file_path)
    return datafile_list


# %% Define the functions to parse data
def process_nc_file(*, filepath):
    """
    Read and parse a netcdf file (as per the old format).

    Parameters
    ----------
    filepath : string or Path
        path to the .nc file.

    Returns
    -------
    df : pandas dataframe
        The contents of the .nc file (parsed as per the old format).

    """
    df = None
    with xr.open_dataset(filepath, engine="netcdf4") as ds:
        df = ds.to_dataframe().reset_index()
        df.rename(columns={"valid_time": "time"}, inplace=True)
        df.sort_values(by=["time", "longitude", "latitude"], ascending=[True, True, True], inplace=True)
        df = df.reset_index(drop=True)
    return df


def merge_dataframes(*, df1, df2):
    """
    Merge two dataframes based on common columns.

    Parameters
    ----------
    df1 : pandas dataframe
        The first dataframe.
    df2 : pandas dataframe
        The second dataframe.

    Returns
    -------
    merged_df : pandas dataframe
        The merged dataframe, on columns common between df1 and df2.

    """
    common_columns = list(set(df1.columns) & set(df2.columns))
    merged_df = pd.merge(df1, df2, on=common_columns)
    return merged_df


def process_zip_file(*, zip_path):
    """
    Process a disguised zip file containing .nc files.

    Parameters
    ----------
    zip_path : string or Path
        The path to the data file (.nc or .zip).

    Returns
    -------
    merged_df : pandas dataframe
        The dataframe containing data from all the .nc files in the .zip file.

    """
    merged_df = None
    # Open the zip file
    with zipfile.ZipFile(zip_path, "r") as z:
        for file_name in z.namelist():
            if file_name.endswith(".nc"):
                # Extract and process each .nc file
                with z.open(file_name) as f:
                    temp_path = f"temp_{file_name}"
                    # Create temp .nc file
                    with open(temp_path, "wb") as temp_file:
                        temp_file.write(f.read())
                    # Process temp .nc file
                    current_df = process_nc_file(filepath=temp_path)
                    merged_df = current_df if merged_df is None else merge_dataframes(df1=merged_df, df2=current_df)
                    # Remove temporary file
                    os.remove(temp_path)
    return merged_df


def parse_data(*, input_file, save_as_nc=False):
    """
    Read the ERA5 downloaded data, parse it and save in the form of a netcdf file.

    Parameters
    ----------
    input_file : string or Path
        The path to the data file..
    save_as_nc : bool, optional
        Whether or not to save the file as a .nc file. The default is True.

    Returns
    -------
    df : pandas dataframe
        The parsed data file.
    filetype : str
        The inferred type of the file at the path `input_file`.

    """
    file_type = magic.from_file(str(input_file))

    if "Zip archive" in file_type:
        logging.info(f"{input_file} is a zip file. Processing its contents...")
        newname = str(input_file).replace(".nc", ".zip")
        input_file.rename(newname)
        df = process_zip_file(zip_path=newname)
        filetype = "zip"
    else:
        logging.info(f"{input_file} is a NetCDF file. Processing directly...")
        df = process_nc_file(filepath=input_file)
        filetype = "nc"
    if save_as_nc:
        # Save the parsed DataFrame as a NetCDF file
        ds = xr.Dataset.from_dataframe(df)
        ds.to_netcdf(str(input_file).replace(".zip", ".nc"))
    return df, filetype


def std_data(*, filepath):
    """
    Standardize a .nc file.

    Parameters
    ----------
    filepath : string or Path
        The path to the .nc file.

    Returns
    -------
    None.

    """
    df, filetype = parse_data(input_file=filepath)
    if "nc" in filetype:
        df_xr = df.set_index(["longitude", "latitude", "time"]).to_xarray()
        filepath = Path(str(filepath).replace(".zip", ".nc"))
        df_xr.to_netcdf(filepath)
    return None


# %% Functions to delete files
def del_file_with_name(*, folder_path, file_name_string):
    """
    Delete all files beginning with file_name_string.

    Parameters
    ----------
    folder_path : str or Path
        The folder within which we are looking for the file.
    file_name_string : str
        The string pattern that we want to look for in a file name (to delete the file).

    Returns
    -------
    None.

    """
    # Iterate through all files in the folder and its subfolders
    for root, _, files in os.walk(folder_path):
        for filename in files:
            # Check if the file name starts with the file_name_string
            if filename.startswith(file_name_string):
                file_path = os.path.join(root, filename)
                try:
                    os.remove(file_path)
                    logging.info(f"Deleted: {file_path}")
                except Exception as e:
                    logging.info(f"Failed to delete {file_path}: {e}")
    return None


def delete_recent_files(*, base_path):
    """
    Delete all recent files in the specified folder.

    Delete the files that correspond to the current and the immediately previous months,
    irrespective of their extensions.

    Parameters
    ----------
    base_path : str or Path
        The folder within which the files should be deleted.

    Returns
    -------
    None.

    """
    # Get the current date and determine the current and previous months
    now = dt.datetime.now()
    current_year = now.year
    current_month = now.month

    # Calculate the previous month, handling year rollover
    if current_month == 1:
        prev_year = current_year - 1
        prev_month = 12
    else:
        prev_year = current_year
        prev_month = current_month - 1

    # Format year and month for matching
    current_month_str = f"{current_year}_{current_month:02d}"
    previous_month_str = f"{prev_year}_{prev_month:02d}"
    del_file_with_name(folder_path=base_path, file_name_string=current_month_str)
    del_file_with_name(folder_path=base_path, file_name_string=previous_month_str)
    return None


# %% Functions for data parsing
def compute_hourly_estimates(group, region_centroid, target_vars):
    """
    Compute estimates for the specified target variables at the given centroid for a group.

    Parameters
    ----------
    group : pandas dataframe
        DataFrame for a specific time group.
    region_centroid : tuple
        Coordinates of the centroid (x, y).
    target_vars : list
        List of target variable names to predict.

    Returns
    -------
    estimated_values : pandas Series
        The hourly estimates of the target variable at centroids.

    """
    # Exclude grouping columns ('time') if included in the group
    group = group[["longitude", "latitude"] + target_vars]
    features = group[["longitude", "latitude"]].values
    centroid_coords = np.array([[region_centroid.x, region_centroid.y]])

    estimates = {}
    for target in target_vars:
        # Extract target values
        target_values = group[target].values

        # Fit the linear regression model
        model = LinearRegression()
        model.fit(features, target_values)

        # Predict at the centroid
        estimates[target] = model.predict(centroid_coords)[0]
    estimated_values = pd.Series(estimates)
    return estimated_values


def estimate_values_at_centroids(*, target_vars, region_data, region_centroid):
    """
    Estimate the parameter values at centroids across time.

    Parameters
    ----------
    target_vars : list (of str)
        List of weather parameters of interest.
    region_data : DataFrame or GeoDataFrame
        The (Geo)DataFrame containing the region specific weather parameter information.
    region_centroid : tuple or Point
        The coordinates of the region centroid.

    Returns
    -------
    df : DataFrame
        The dataframe containig the weather information at region centroids.

    """
    # Ensure 'time' column is in datetime format
    region_data["time"] = pd.to_datetime(region_data["time"])

    # Group by 'time' and apply the custom function
    hourly_estimates = (
        region_data.groupby(["time"])[["time", "latitude", "longitude"] + target_vars]
        .apply(compute_hourly_estimates, region_centroid=region_centroid, target_vars=target_vars)
        .reset_index()
    )
    return hourly_estimates


def process_month_data(*, filepath, w_params, region_gdf_list, region_centroid_list):
    """
    Process a datafile and return the values at region centroids.

    Parameters
    ----------
    filepath : str or Path
        The path to the file of interest.
    w_params : list (of str)
        List of weather parameters of interest.
    region_gdf_list : list (of geopandas dataframes)
        The list of GeoPandas Dataframe containing the region boundaries.
    region_centroid_list : TYPE
        The GeoPandas Dataframe containing the region centroids.

    Returns
    -------
    all_centroid_data : pandas dataframe
        The parameters at region centroids at each hour every day.

    """
    # Read and parse the raw data
    df, filetype = parse_data(input_file=filepath, save_as_nc=False)
    # Convert it into a geopandas dataframe
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
    # centroid_values0 = [estimate_values_at_centroids(w_params, v1, v2) for v1, v2 in zip(region_data_list, region_centroid_list)]
    # centroid_values = []
    # for v1, v2 in zip(centroid_values0, region_gdf_list):
    #     v1.loc[:, 'region_id'] = v2.loc[0, 'region_id']
    #     v1.loc[:, 'name'] = v2.loc[0, 'name']
    #     v1.loc[:, 'parent'] = v2.loc[0, 'parent']
    #     v1.loc[:, 'parent_name'] = v2.loc[0, 'parent_name']
    #     centroid_values.append(v1)
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
    return all_centroid_data


def parse_data_region_this(*, src_file, dest_file, w_params, region_gdf_list, region_centroid_list, geojson_path, region_type):
    """
    Process a data (source) file and output a parsed (destination) file.

    The parsed file is region specific.

    Parameters
    ----------
    src_file : str or Path
        Path to the source file.
    dest_file : str or Path
        The destination file.
    w_params : list (of str)
        List of weather parameters of interest.
    region_gdf_list : list of GeoPandas Dataframes
        List of geopandas dataframes containing information of the regions of interest.
    region_centroid_list : list of GeoPandas Points
        List of geopandas Points corresponding to centroids of the regions of interest.
    geojson_path : str or Path
        The geojson folder containing the region geojson files.
    region_type: str
        The type of the region, district or subdistrict, for which the parsing is being carried out.

    Returns
    -------
    None.

    """
    if (region_gdf_list is None) and (region_centroid_list is None) and (geojson_path is not None):
        geojson_path = Path(geojson_path)
        region_gdf_list = get_region_gdfs(geojson_folder=geojson_path, region_type=region_type, target_crs="EPSG:4326")
        region_centroid_list = [get_region_centroids(gdf=gdf)["geometry"][0] for gdf in region_gdf_list]
    try:
        all_centroid_data = process_month_data(
            filepath=src_file, w_params=w_params, region_gdf_list=region_gdf_list, region_centroid_list=region_centroid_list
        )
        # Save the file to the destination location
        all_centroid_data.to_csv(dest_file)
        logging.info(f"Processed and saved: {dest_file}")
    except Exception as e:
        logging.info(f"Error processing {src_file}: {e}")
    return None


def parse_region_data_all(*, source_folder, destination_folder, geojson_folder, w_params, region_type="district"):
    """
    Parse all data files.

    Read .nc/.zip files from the source folder, compute values at region centroids,
    and save them as .csv into the destination folder, maintaining the same folder structure.

    Parameters
    ----------
        source_folder : str or Path
            Path to the source folder containing yearly subfolders with .nc files.
        destination_folder : str or Path
            The destination folder where the processed files will be saved.
        geojson_folder : str or Path
            The geojson folder containing the region geojson files.
        w_params : list (of str)
            List of weather parameters of interest.
        region_type: str
            The type of the region, district or subdistrict, for which the parsing is being carried out.

    Returns
    -------
        None

    """
    source_path = Path(source_folder)
    destination_path = Path(destination_folder) / region_type
    geojson_path = Path(geojson_folder)
    # Get the list of geojson files using which we want to: (a) compute centroids, (b) fetch required weather data points
    region_gdf_list = get_region_gdfs(geojson_folder=geojson_path, region_type=region_type, target_crs="EPSG:4326")
    region_centroid_list = [get_region_centroids(gdf=gdf)["geometry"][0] for gdf in region_gdf_list]
    list_data_files = get_datafile_list(source_path=source_path, filetypes=[".nc", ".zip"])
    # all_regions_gdf = gpd.GeoDataFrame(pd.concat(regions_list, ignore_index=True))
    # region_gdf = all_regions_gdf.dissolve(by='parent_name').reset_index()

    # Traverse all .nc files in the source folder
    for this_file in list_data_files:
        # Calculate the relative path from the source folder
        relative_path = str(this_file.relative_to(source_path))
        # Determine the corresponding source path
        src_file = source_path / Path(relative_path)
        # Determine the corresponding destination path
        dest_file = destination_path / Path(relative_path.replace(".nc", ".csv").replace(".zip", ".csv"))
        # Ensure the parent directory exists
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        # Process the file from the source location
        parse_data_region_this(
            src_file=src_file,
            dest_file=dest_file,
            w_params=w_params,
            region_gdf_list=region_gdf_list,
            region_centroid_list=region_centroid_list,
            geojson_path=geojson_path,
            region_type=region_type,
        )
    return None


def parse_region_data_from_to(*, ym_i, ym_j, source_folder, destination_folder, geojson_folder, w_params, region_type="district"):
    """
    Parse data for a region between i-th (year, month) and j-th (year, month), both included.

    Read .nc/.zip files from the source folder, compute values at region centroids,
    and save them as .csv into the destination folder, maintaining the same folder structure.

    Parameters
    ----------
    ym_i : tuple (of integers)
        The (year, month) from to which the data is to be downloaded.
    ym_j : tuple (of integers)
        The (year, month) up to which the data is to be downloaded.
    source_folder : str or Path
        Path to the source folder containing yearly subfolders with .nc files.
    destination_folder : str or Path
        The destination folder where the processed files will be saved.
    geojson_folder : str or Path
        The geojson folder containing the region geojson files.
    w_params : list (of str)
        List of weather parameters of interest.
    region_type: str
        The type of the region, district or subdistrict, for which the parsing is being carried out.

    Returns
    -------
        None
    """
    source_path = Path(source_folder)
    destination_path = Path(destination_folder) / region_type
    geojson_path = Path(geojson_folder)
    # Get the list of geojson files using which we want to: (a) compute centroids, (b) fetch required weather data points
    region_gdf_list = get_region_gdfs(geojson_folder=geojson_path, region_type=region_type, target_crs="EPSG:4326")
    region_centroid_list = [get_region_centroids(gdf=gdf)["geometry"][0] for gdf in region_gdf_list]
    list_data_files = get_datafile_list(source_path=source_path, filetypes=[".nc", ".zip"])

    years = np.arange(ym_i[0], ym_j[0] + 1)
    months = np.arange(1, 13)
    yearmonth = [(y, m) for y in years for m in months if (([y, m] >= [ym_i[0], ym_i[1]]) and ([y, m] <= [ym_j[0], ym_j[1]]))]
    yearmonth_strings = [f"{val[0]}_{val[1]:02}" for val in yearmonth]
    # Filter out the unnecessary files
    list_data_files_necessary = []
    for this_string in yearmonth_strings:
        check_if_file_exists = False
        for val in list_data_files:
            if this_string in str(val) and (".zip" in str(val) or ".nc" in str(val)):
                list_data_files_necessary.append(val)
                check_if_file_exists = True
        if not check_if_file_exists:
            logging.info(f"There is no data file for {this_string}")

    # Traverse all .nc files in the source folder
    for this_file in list_data_files_necessary:
        # Calculate the relative path from the source folder
        relative_path = str(this_file.relative_to(source_path))
        # Determine the corresponding source path
        src_file = source_path / Path(relative_path)
        # Determine the corresponding destination path
        dest_file = destination_path / Path(relative_path.replace(".nc", ".csv").replace(".zip", ".csv"))
        # Ensure the parent directory exists
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        # Process the file from the source location
        parse_data_region_this(
            src_file=src_file,
            dest_file=dest_file,
            w_params=w_params,
            region_gdf_list=region_gdf_list,
            region_centroid_list=region_centroid_list,
            geojson_path=geojson_path,
            region_type=region_type,
        )
    return None


def parse_region_data_recent(source_folder, destination_folder, geojson_folder, w_params, region_type="district"):
    """
    Parse data for a region for the recent (current and immediately previous) months.

    Read .nc/.zip files from the source folder, compute values at region centroids,
    and save them as .csv into the destination folder, maintaining the same folder structure.

    Parameters
    ----------
    source_folder : str or Path
        Path to the source folder containing yearly subfolders with .nc files.
    destination_folder : str or Path
        The destination folder where the processed files will be saved.
    geojson_folder : str or Path
        The geojson folder containing the region geojson files.
    w_params : list (of str)
        List of weather parameters of interest.
    region_type: str
        The type of the region, district or subdistrict, for which the parsing is being carried out.

    Returns
    -------
        None
    """
    # Get the current date and determine the current and previous months
    now = dt.datetime.now()
    current_year = now.year
    current_month = now.month

    # Calculate the previous month, handling year rollover
    if current_month == 1:
        prev_year = current_year - 1
        prev_month = 10
    elif current_month == 2:
        prev_year = current_year - 1
        prev_month = 11
    elif current_month == 3:
        prev_year = current_year - 1
        prev_month = 12
    else:
        prev_year = current_year
        prev_month = current_month - 3

    # Format year and month for matching
    ym_i = (prev_year, prev_month)
    ym_j = (current_year, current_month)

    # ym_i, ym_j, source_folder, destination_folder, geojson_folder, w_params, region_type='district'
    parse_region_data_from_to(
        ym_i=ym_i,
        ym_j=ym_j,
        source_folder=source_folder,
        destination_folder=destination_folder,
        geojson_folder=geojson_folder,
        w_params=w_params,
        region_type=region_type,
    )
    return None


# Functions for daily data aggregation
def check_file_existence(*, file_name):
    """
    Check if a file exists. If not, warn.

    Parameters
    ----------
    file_name : str or Path
        The file for which we want to check for existence.

    Returns
    -------
    bool
        True if the file exists, False otherwise.

    """
    if not os.path.exists(file_name):
        logging.info(f"Warning: File {file_name} does not exist. Skipping.")
        return False
    else:
        return True


def ret_prev_data_file_names(*, this_file, N):
    """
    Given a file, identify all the potential files containing data for prior dates.

    The prior files depend on the number of prior N days for which we want to aggregata data.

    Parameters
    ----------
    this_file : str or Path
        The file for which we want to check and list files containing data for prior days.
    N : int
        The number of prior days (including the day of intrest) for which we want to aggregate data.

    Returns
    -------
    file_names : list (of Path)
        List of files containing data for prior days.

    """
    if check_file_existence(file_name=this_file):
        # Extract year, month, and extension from the input file
        file_name_string = Path(this_file).name
        file_name, ext = file_name_string.split(".")
        base_name = str(this_file)
        year, month = map(int, file_name_string.split("_")[:-1])

        # Calculate the starting date for the N days range
        current_date = dt.datetime(year, month, 1)
        start_date = current_date - dt.timedelta(days=N)

        # Generate all months from start_date to the current date's month
        file_names = []
        while start_date <= current_date:
            file_name = base_name.replace(f"{year}", f"{start_date.year:04d}").replace(f"_{month:02d}", f"_{start_date.month:02d}")
            # file_name = f"{start_date.year:04d}_{start_date.month:02d}{ext}"
            if Path(file_name) not in file_names:
                file_names.append(Path(file_name))
            # Move to the next month
            if start_date.month == 12:
                start_date = dt.datetime(start_date.year + 1, 1, 1)
            else:
                start_date = dt.datetime(start_date.year, start_date.month + 1, 1)
        # # Add the next month
        # if current_date.month == 12:
        #     next_date = dt.datetime(current_date.year + 1, 1, 1)
        # else:
        #     next_date = dt.datetime(current_date.year, current_date.month + 1, 1)
        # file_name = base_name.replace(f'{year}', f'{next_date.year:04d}').replace(f'_{month:02d}', f'_{next_date.month:02d}') + ext
        # file_names.append(Path(file_name))
        file_names = [val for val in file_names if check_file_existence(file_name=val)]
    else:
        logging.info(f"Your input file: {this_file} does not exist. Please check the path!")
        file_names = []
    return file_names


def merge_file_daily_data(*, file_names, w_params):
    """
    Merge data from multiple files and do time correction to IST.

    Remove incomplete data for the first and the last dates after time correction to IST.
    This is required to carry aggregation over N days.

    Parameters
    ----------
    file_names : list of str (or Path)
        List of relevant file names from which we want to merge data.
    w_params : list of str
        List of parameters of interest.

    Returns
    -------
    merged_df : pandas dataframe
        The dataframe containing data from multiple files.

    """
    list_data = []
    cols1 = ["time", "region_id", "name", "parent", "parent_name"]
    cols = cols1 + w_params
    for thisfile in file_names:
        thisdf = pd.read_csv(thisfile, index_col=0)
        thisdf["time"] = pd.to_datetime(thisdf["time"], errors="coerce")
        # Convert time from GMT to IST
        thisdf["time"] = thisdf["time"] + dt.timedelta(hours=5.5)
        thisdf = thisdf[cols]  # Reorder columns
        list_data.append(thisdf)
    merged_df = pd.concat(list_data)
    merged_df = merged_df.sort_values(cols1, ascending=[True] * len(cols1)).reset_index(drop=True)
    merged_df.loc[:, "date"] = merged_df["time"].apply(lambda x: x.date())
    cols1[0] = "date"
    cols = cols1 + w_params
    merged_df = merged_df[cols]
    list_dates = list(merged_df["date"].unique())
    list_dates = list_dates[1:-1]
    # Remove the first and the last dates because they contain partial information of a day because of IST correction
    merged_df = merged_df[merged_df["date"].isin(list_dates)].reset_index(drop=True)
    merged_df = merged_df.sort_values(["region_id", "date"], ascending=[True, True]).reset_index(drop=True)
    return merged_df


def aggregate_daily(*, this_file, common_cols, w_params, operations):
    """
    Aggregate data daily for the given file (assume that a file contains data for a month).

    1. For boundary dates go to previous month file (if it exists).
    2. Merge current and previous month file data (happens in merge_file_daily_data):
            a. Do time correction to covert to IST.
            b. Filter out boundary dates because they have incomplete day information due to IST correction.
    3. Aggregate data over each day.
    4. Keep data only for the month of interest.

    Parameters
    ----------
    this_file : str or Path
         The path to the file for which we want to do aggregation.
    common_cols : list of str
        The columns whose values are not aggregated. That is, we can group by all such columns.
    w_params : list of str
        The columns whose values are aggregated.
    operation : list of str
        The operations that are operated on each of the w_params elements.

    Returns
    -------
    agg_daily_df : pandas dataframe
        The dataframe containing the aggregated data.

    """
    # Fetch all file names from which we may need to get data
    file_names = ret_prev_data_file_names(this_file=this_file, N=1)
    # Merge the relevant data files
    w_params_unique = []
    for val in w_params:
        if val not in w_params_unique:
            w_params_unique.append(val)
    merged_df = merge_file_daily_data(file_names=file_names, w_params=w_params_unique)
    # Aggregate for each day
    mapped = {f"{col}_{func}": (col, func) for col, func in zip(w_params, operations)}
    agg_daily_df = merged_df.groupby(common_cols, as_index=False).agg(**mapped)
    agg_daily_df = agg_daily_df.sort_values(["region_id", "date"], ascending=[True, True]).reset_index(drop=True)
    # Filter out the unnecessary dates
    file_name_string = Path(this_file).name
    file_name, ext = file_name_string.split(".")
    year, month = map(int, file_name_string.split("_")[:-1])
    ref_date = dt.datetime(year, month, 1).date()
    agg_daily_df = agg_daily_df[agg_daily_df["date"] >= ref_date].reset_index(drop=True)
    return agg_daily_df


def aggregate_daily_this(*, src_file, dest_file, common_cols, w_params, operations):
    """
    Aggregate the data of a source file over N days and save in a destination file.

    Parameters
    ----------
    src_file : str or Path
        The path to the source file.
    dest_file : str or Path
        The path to the destination file.
    common_cols : list of str
        The columns whose values are not aggregated. That is, we can group by all such columns.
    w_params : list of str
        The columns whose values are aggregated.
    operation : list of str
        The operations that are operated on each of the w_params elements.

    Returns
    -------
    None.

    """
    # Infer destination file name using the source file name, and soure and destination folder names.
    if check_file_existence(file_name=src_file):
        df_agg_daily = aggregate_daily(this_file=src_file, common_cols=common_cols, w_params=w_params, operations=operations)
        df_agg_daily.to_csv(dest_file)
    else:
        logging.info(f"{str(src_file)} does not exist! Check file path!")
    return None


def aggregate_daily_all(*, source_folder, destination_folder, common_cols, w_params, operations):
    """
    Aggregate data for a region between for all the months (in separate file for each month).

    Read parsed .csv files from the source folder, aggregate values for each day
    and save them as .csv into the destination folder, maintaining the same folder structure.

    Parameters
    ----------
    source_folder : str or Path
        The source folder containing the files, the contents of which we want to aggregate.
    destination_folder : str or Path
        The destination folder in which we want to save all the aggregated data.
    common_cols : list of str
        The columns whose values are not aggregated. That is, we can group by all such columns.
    w_params : list of str
        The columns whose values are aggregated.
    operation : list of str
        The operations that are operated on each of the w_params elements.

    Returns
    -------
    None.

    """
    source_path = Path(source_folder)
    destination_path = Path(destination_folder)
    list_data_files = get_datafile_list(source_path=source_path, filetypes=[".csv"])

    # Traverse all .csv files in the source folder
    for this_file in list_data_files:
        # Calculate the relative path from the source folder
        relative_path = str(this_file.relative_to(source_path))
        # Determine the corresponding source path
        src_file = source_path / Path(relative_path)
        # Determine the corresponding destination path
        dest_file = destination_path / Path(relative_path)
        # Ensure the parent directory exists
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        # Process the file from the source location
        aggregate_daily_this(src_file=src_file, dest_file=dest_file, common_cols=common_cols, w_params=w_params, operations=operations)
    return None


def aggregate_daily_from_to(*, ym_i, ym_j, source_folder, destination_folder, common_cols, w_params, operations):
    """
    Aggregate data for a region between i-th (year, month) and j-th (year, month), both included.

    Read parsed .csv files from the source folder, aggregate values for each day
    and save them as .csv into the destination folder, maintaining the same folder structure.

    Parameters
    ----------
    ym_i : tuple (of integers)
        The (year, month) from to which the data is to be downloaded.
    ym_j : tuple (of integers)
        The (year, month) up to which the data is to be downloaded.
    source_folder : str or Path
        Path to the source folder containing yearly subfolders with .nc files.
    destination_folder : str or Path
        The destination folder where the processed files will be saved.
    common_cols : list of str
        The columns whose values are not aggregated. That is, we can group by all such columns.
    w_params : list of str
        The columns whose values are aggregated.
    operation : list of str
        The operations that are operated on each of the w_params elements.

    Returns
    -------
        None
    """
    source_path = Path(source_folder)
    destination_path = Path(destination_folder)
    list_data_files = get_datafile_list(source_path=source_path, filetypes=[".csv"])

    years = np.arange(ym_i[0], ym_j[0] + 1)
    months = np.arange(1, 13)
    yearmonth = [(y, m) for y in years for m in months if (([y, m] >= [ym_i[0], ym_i[1]]) and ([y, m] <= [ym_j[0], ym_j[1]]))]
    yearmonth_strings = [f"{val[0]}_{val[1]:02}" for val in yearmonth]
    # Filter out the unnecessary files
    list_data_files_necessary = []
    for this_string in yearmonth_strings:
        check_if_file_exists = False
        for val in list_data_files:
            if this_string in str(val):
                list_data_files_necessary.append(val)
                check_if_file_exists = True
        if not check_if_file_exists:
            logging.info(f"There is no data file for {this_string}")

    # Traverse all .csv files in the source folder
    for this_file in list_data_files_necessary:
        # Calculate the relative path from the source folder
        relative_path = str(this_file.relative_to(source_path))
        # Determine the corresponding source path
        src_file = source_path / Path(relative_path)
        # Determine the corresponding destination path
        dest_file = destination_path / Path(relative_path)
        # Ensure the parent directory exists
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        # Process the file from the source location
        aggregate_daily_this(src_file=src_file, dest_file=dest_file, common_cols=common_cols, w_params=w_params, operations=operations)
    return None


def aggregate_daily_recent(*, source_folder, destination_folder, common_cols, w_params, operations):
    """
    Aggregate data for a region for the recent (current and immediately previous) months.

    Read parsed .csv files from the source folder, aggregate values for each day
    and save them as .csv into the destination folder, maintaining the same folder structure.

    Parameters
    ----------
    source_folder : str or Path
        Path to the source folder containing yearly subfolders with .nc files.
    destination_folder : str or Path
        The destination folder where the processed files will be saved.
    common_cols : list of str
        The columns whose values are not aggregated. That is, we can group by all such columns.
    w_params : list of str
        The columns whose values are aggregated.
    operation : list of str
        The operations that are operated on each of the w_params elements.

    Returns
    -------
        None
    """
    # Get the current date and determine the current and previous months
    now = dt.datetime.now()
    current_year = now.year
    current_month = now.month

    # Calculate the previous month, handling year rollover
    if current_month == 1:
        prev_year = current_year - 1
        prev_month = 12
    else:
        prev_year = current_year
        prev_month = current_month - 1

    # Format year and month for matching
    ym_i = (prev_year, prev_month)
    ym_j = (current_year, current_month)

    aggregate_daily_from_to(
        ym_i=ym_i,
        ym_j=ym_j,
        source_folder=source_folder,
        destination_folder=destination_folder,
        common_cols=common_cols,
        w_params=w_params,
        operations=operations,
    )
    return None


# %% Functions for data aggregation for N-days
def merge_file_data_Ndays(*, file_names, common_cols, w_params, N=7):
    """
    Merge data from multiple files.

    Parameters
    ----------
    file_names : list of str (or Path)
        List of relevant file names from which we want to merge data.
    common_cols : list of str
        The columns whose values are not aggregated. That is, we can group by all such columns.
    w_params : list of str
        List of parameters of interest.
    N : int, optional
        The number of days for which we want the rolling (avg or sum) values. The default is 7.

    Returns
    -------
    merged_df : pandas dataframe
        The dataframe containing data from multiple files.

    """
    list_data = []
    cols = common_cols + w_params
    for thisfile in file_names:
        thisdf = pd.read_csv(thisfile, index_col=0)
        thisdf["date"] = pd.to_datetime(thisdf["date"], errors="coerce")
        thisdf = thisdf[cols]  # Reorder columns
        list_data.append(thisdf)
    merged_df = pd.concat(list_data)
    merged_df = merged_df.sort_values(common_cols, ascending=[True] * len(common_cols)).reset_index(drop=True)
    merged_df = merged_df[cols]
    merged_df = merged_df.sort_values(["region_id", "date"], ascending=[True, True]).reset_index(drop=True)
    return merged_df


def rolling_aggregate_Ndays(*, df, common_cols, w_params, operations, N=7):
    """
    Aggregate the data in a rolling manner (previous N data points including the given day) for each day.

    Parameters
    ----------
    df : pandas dataframe
        Dataframe containing daily data.
    common_cols : list of str
        The columns whose values are not aggregated. That is, we can group by all such columns.
    w_params : list of str
        The columns whose values are aggregated.
    operation : list of str
        The operations that are operated on each of the w_params elements.
    N : int, optional
        The number of days for which we want the rolling (avg or sum) values. The default is 7.

    Returns
    -------
    pandas datafrane
        The dataframe containing rolling N-day aggregated values.

    """

    def rolling_fn(group, common_cols=common_cols, w_params=w_params, operations=operations):
        group = group.sort_values("date").reset_index()  # Ensure data is sorted by date
        group = group.set_index("date")  # Set date as index for rolling operation
        # Perform rolling aggregation on numeric columns
        rolling_df = group[w_params].rolling(f"{N - 1}D", closed="both").agg({v1: v2 for v1, v2 in zip(w_params, operations)}).reset_index()
        # Add common columns to rolling_df
        rolling_df = rolling_df.merge(group.reset_index()[common_cols], on="date", how="left")
        return rolling_df

    df["date"] = pd.to_datetime(df["date"])
    listcols = common_cols + w_params
    df_aggN = df.groupby("region_id")[listcols].apply(rolling_fn).reset_index(drop=True)
    df_aggN = df_aggN[listcols]
    return df_aggN


def aggregate_Ndays(*, this_file, common_cols, w_params, operations, N=7):
    """
    Aggregate data over an N day period for the given file (assume that a file contains data for a month).

    1. For boundary dates go to previous month file (if it exists).
    2. Merge current and previous month(s) file data.
    3. Aggregate data over previous N days (N days include the day of interest).
    4. Keep data only for the month of interest.

    Parameters
    ----------
    this_file : str or Path
         The path to the file for which we want to do aggregation.
    common_cols : list of str
        The columns whose values are not aggregated. That is, we can group by all such columns.
    w_params : list of str
        The columns whose values are aggregated.
    operation : list of str
        The operations that are operated on each of the w_params elements.
    N : int, optional
        The number of prior days (including the day of interest) for which we want to do aggregation. The default is 7.

    Returns
    -------
    af_aggN : pandas dataframe
        The dataframe containing the aggregated data.

    """
    # Fetch all file names from which we may need to get data
    file_names = ret_prev_data_file_names(this_file=this_file, N=N)
    # Merge the relevant data files
    merged_df = merge_file_data_Ndays(file_names=file_names, common_cols=common_cols, w_params=w_params)
    # Aggregate over past N days (including the day of interest)
    df_aggN = rolling_aggregate_Ndays(df=merged_df, common_cols=common_cols, w_params=w_params, operations=operations, N=N)
    # Filter out the unnecessary dates
    # Whats this doing?
    file_name_string = Path(this_file).name
    file_name, ext = file_name_string.split(".")
    year, month = map(int, file_name_string.split("_")[:-1])
    ref_date = pd.to_datetime(f"{year}-{month}-1")
    df_aggN = df_aggN[df_aggN["date"] >= ref_date].reset_index(drop=True)
    # Return the aggregate information
    return df_aggN


def aggregate_Ndays_this(*, src_file, dest_file, common_cols, w_params, operations, N=7):
    """
    Aggregate the data of a source file over N days and save in a destination file.

    Parameters
    ----------
    src_file : str or Path
        The path to the source file.
    dest_file : str or Path
        The path to the destination file.
    common_cols : list of str
        The columns whose values are not aggregated. That is, we can group by all such columns.
    w_params : list of str
        The columns whose values are aggregated.
    operation : list of str
        The operations that are operated on each of the w_params elements.
    N : int, optional
        The number of prior days (including the day of interest) for which we want to do aggregation. The default is 7.

    Returns
    -------
    None.

    """
    if check_file_existence(file_name=src_file):
        df_aggN = aggregate_Ndays(this_file=src_file, common_cols=common_cols, w_params=w_params, operations=operations, N=N)
        df_aggN.to_csv(dest_file)
    else:
        logging.info(f"{str(src_file)} does not exist! Check file path!")
    return None


def aggregate_Ndays_all(*, source_folder, destination_folder, common_cols, w_params, operations, N=7):
    """
    Aggregate data for a region between for all the months (in separate file for each month).

    Read .csv files from the source folder, aggregate values for N days
    and save them as .csv into the destination folder, maintaining the same folder structure.

    Parameters
    ----------
    source_folder : str or Path
        The source folder containing the files, the contents of which we want to aggregate.
    destination_folder : str or Path
        The destination folder in which we want to save all the aggregated data.
    common_cols : list of str
        The columns whose values are not aggregated. That is, we can group by all such columns.
    w_params : list of str
        The columns whose values are aggregated.
    operation : list of str
        The operations that are operated on each of the w_params elements.
    N : int, optional
        The number of prior days (including the day of interest) for which we want to do aggregation. The default is 7.

    Returns
    -------
    None.

    """
    source_path = Path(source_folder)
    destination_path = Path(destination_folder)
    list_data_files = get_datafile_list(source_path=source_path, filetypes=[".csv"])

    # Traverse all .csv files in the source folder
    # Infer destination file name using the source file name, and soure and destination folder names.
    for this_file in list_data_files:
        # Calculate the relative path from the source folder
        relative_path = str(this_file.relative_to(source_path))
        # Determine the corresponding source path
        src_file = source_path / Path(relative_path)
        # Determine the corresponding destination path
        dest_file = destination_path / Path(relative_path)
        # Ensure the parent directory exists
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        # Process the file from the source location
        aggregate_Ndays_this(src_file=src_file, dest_file=dest_file, common_cols=common_cols, w_params=w_params, operations=operations, N=N)
    return None


def aggregate_Ndays_from_to(*, ym_i, ym_j, source_folder, destination_folder, common_cols, w_params, operations, N=7):
    """
    Aggregate data for a region between i-th (year, month) and j-th (year, month), both included.

    Read .csv files from the source folder, aggregate values for N days
    and save them as .csv into the destination folder, maintaining the same folder structure.

    Parameters
    ----------
    ym_i : tuple (of integers)
        The (year, month) from to which the data is to be downloaded.
    ym_j : tuple (of integers)
        The (year, month) up to which the data is to be downloaded.
    source_folder : str or Path
        Path to the source folder containing yearly subfolders with .nc files.
    destination_folder : str or Path
        The destination folder where the processed files will be saved.
    common_cols : list of str
        The columns whose values are not aggregated. That is, we can group by all such columns.
    w_params : list of str
        The columns whose values are aggregated.
    operation : list of str
        The operations that are operated on each of the w_params elements.
    N : int
        The number of days for whch aggregatuion is done.

    Returns
    -------
        None
    """
    source_path = Path(source_folder)
    destination_path = Path(destination_folder)
    list_data_files = get_datafile_list(source_path=source_path, filetypes=[".csv"])

    years = np.arange(ym_i[0], ym_j[0] + 1)
    months = np.arange(1, 13)
    yearmonth = [(y, m) for y in years for m in months if (([y, m] >= [ym_i[0], ym_i[1]]) and ([y, m] <= [ym_j[0], ym_j[1]]))]
    yearmonth_strings = [f"{val[0]}_{val[1]:02}" for val in yearmonth]
    # Filter out the unnecessary files
    list_data_files_necessary = []
    for this_string in yearmonth_strings:
        check_if_file_exists = False
        for val in list_data_files:
            if this_string in str(val):
                list_data_files_necessary.append(val)
                check_if_file_exists = True
        if not check_if_file_exists:
            logging.info(f"There is no data file for {this_string}")

    # Traverse all .csv files in the source folder
    for this_file in list_data_files_necessary:
        # Calculate the relative path from the source folder
        relative_path = str(this_file.relative_to(source_path))
        # Determine the corresponding source path
        src_file = source_path / Path(relative_path)
        # Determine the corresponding destination path
        dest_file = destination_path / Path(relative_path)
        # Ensure the parent directory exists
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        # Process the file from the source location
        aggregate_Ndays_this(src_file=src_file, dest_file=dest_file, common_cols=common_cols, w_params=w_params, operations=operations, N=N)
    return None


def aggregate_Ndays_recent(*, source_folder, destination_folder, common_cols, w_params, operations, N=7):
    """
    Aggregate data for a region for the recent (current and immediately previous) months.

    Read .csv files from the source folder, aggregate values for N days
    and save them as .csv into the destination folder, maintaining the same folder structure.

    Parameters
    ----------
    source_folder : str or Path
        Path to the source folder containing yearly subfolders with .nc files.
    destination_folder : str or Path
        The destination folder where the processed files will be saved.
    common_cols : list of str
        The columns whose values are not aggregated. That is, we can group by all such columns.
    w_params : list of str
        The columns whose values are aggregated.
    operation : list of str
        The operations that are operated on each of the w_params elements.
    N : int
        The number of days for whch aggregatuion is done.

    Returns
    -------
        None
    """
    # Get the current date and determine the current and previous months
    now = dt.datetime.now()
    current_year = now.year
    current_month = now.month

    # Calculate the previous month, handling year rollover
    if current_month == 1:
        prev_year = current_year - 1
        prev_month = 12
    else:
        prev_year = current_year
        prev_month = current_month - 1

    # Format year and month for matching
    ym_i = (prev_year, prev_month)
    ym_j = (current_year, current_month)

    # Aggregate data from ym_i to ym_j
    aggregate_Ndays_from_to(
        ym_i=ym_i,
        ym_j=ym_j,
        source_folder=source_folder,
        destination_folder=destination_folder,
        common_cols=common_cols,
        w_params=w_params,
        operations=operations,
        N=N,
    )
    return None


# Check if some dates are missing in a dataframe
def find_missing_dates(*, df, column_name):
    """
    Identify missing dates in a date column of a DataFrame.

    Parameters
    ----------
    df : pandas dataframe
        Input DataFrame.
    column_name : str
        Name of the column containing date values.

    Returns
    -------
    missing_dates : list (of pd.Timestamp)
        Sorted list of missing dates.

    Raises
    ------
    ValueError: If any date in the column cannot be converted to Timestamp (NaT).
    """
    df = deepcopy(df)
    df[column_name] = pd.to_datetime(df[column_name], errors="coerce")

    # Check for NaT values and raise an error
    if df[column_name].isna().any():
        raise ValueError("Column contains NaT values after conversion to Timestamp. Check for invalid or missing dates.")

    date_series = df[column_name].drop_duplicates().sort_values()  # Sort and remove duplicates
    if date_series.empty:  # Check if empty
        return []

    # Find missing dates
    full_range = pd.date_range(start=date_series.min(), end=date_series.max(), freq="D")  # Create full date range
    missing_dates = full_range.difference(date_series)
    missing_dates = list(missing_dates)

    return missing_dates


# Club all aggregated data together
def merge_aggregate_data(*, source_folder, destination_path):
    """
    Merge all aggregated data for a region.

    Read .csv files from the source folder and save the merged data as .csv into the destination path.

    Parameters
    ----------
    source_folder : str or Path
        The source folder containing the files, the contents of which we want to merge.
    destination_path : str or Path
        The destination path in which we want to save merged aggregated data.

    Returns
    -------
    df_merged : pandas dataframe
        Contains all the aggregated data for a region in one file.

    """
    source_path = Path(source_folder)
    destination_path = Path(destination_path)
    list_data_files = get_datafile_list(source_path=source_path, filetypes=[".csv"])
    df_list = [pd.read_csv(thisfile, index_col=0) for thisfile in list_data_files]
    df_merged = pd.concat(df_list).reset_index(drop=True)
    df_merged = df_merged.sort_values(["region_id", "date"], ascending=[True, True]).reset_index(drop=True)
    # Check if some dates are missing. If yes, print a warning. BEGINS
    missing_dates = find_missing_dates(df=df_merged, column_name="date")
    if len(missing_dates) != 0:
        missing_dates_string = "\n".join([val.strftime("%Y-%m-%d") for val in missing_dates])
        logging.info(f"WARNING: Some date(s) missing in the aggregated data! Please check the following: \n{missing_dates_string}")
    else:
        logging.info("No dates missing!")
    # Check if some dates are missing. If yes, print a warning. ENDS
    df_merged.to_csv(destination_path)
    return df_merged


# Sample data: The data is for all days. However, model training may require us to sample every N=7 days
def sample_data(
    *,
    filepath,
    sample_filepath,
    start_date: pd.Timestamp | None = None,
    end_date: pd.Timestamp | None = None,
    sample_from: Literal["beginning", "end"] = "end",
    sampling_rate=7,
    sampling_day: str | None = None,
):
    """
    Sample the data from a file at a sampling rate, save the resultant dataframe, and also return it.

    If sampling_day is provided, filters to only that day of week (e.g., all Wednesdays),
    which naturally gives weekly sampling. Otherwise uses the old sampling_rate logic.

    Parameters
    ----------
    filepath : Path or str
        The path to the original file.
    sample_filepath : Path or str
        The path to the sample file.
    start_date : pd.Timestamp | None
        The date from which we seek samples.
    end_date : pd.Timestamp | None
        The date up to which we seek samples.
    sample_from : str, optional
        Do sampling from the start_date or upto end_date. The default is 'end'.
    sampling_rate : int, optional
        The rate at which we want to sample the data (only used if sampling_day is None). The default is 7.
    sampling_day : str, optional
        The day of week to sample (e.g., 'W-MON', 'W-WED'). If provided, only dates matching
        this day of week will be kept, ensuring consistent weekly sampling.

    Returns
    -------
    df_sample : pandas dataframe
        The sample file.

    """
    filepath = Path(filepath)
    sample_filepath = Path(sample_filepath)
    if sample_from not in {"beginning", "end"}:
        raise ValueError(f"'sample_from' must be 'beginning' or 'end', got '{sample_from}'")

    df = pd.read_csv(filepath, index_col=0)
    df["date"] = pd.to_datetime(df["date"])

    # Filter by date range if specified
    if start_date is not None:
        df = df[df["date"] >= pd.Timestamp(start_date)]
    if end_date is not None:
        df = df[df["date"] <= pd.Timestamp(end_date)]

    # If sampling_day is provided, filter to only that day of week
    if sampling_day is not None:
        # Extract day name from sampling_day (e.g., 'W-WED' -> 'WED')
        day_abbr = sampling_day.split("-")[-1]  # Get 'WED' from 'W-WED'
        # Filter to only dates matching this day of week
        df_sample = df[df["date"].dt.strftime("%a").str.upper() == day_abbr].reset_index(drop=True)
    else:
        # Use old logic with sampling_rate
        N = sampling_rate
        list_dates = list(df["date"].unique())
        list_dates = [pd.Timestamp(d) for d in list_dates]
        list_dates.sort()

        if sample_from == "end":
            sample_dates = list_dates[::-1][::N]
        elif sample_from == "beginning":
            sample_dates = list_dates[::N]

        df_sample = df[df["date"].isin(sample_dates)].reset_index(drop=True)

    df_sample.to_csv(sample_filepath)
    return df_sample


# Get day abbreviation. This determines sampling of case and weather data
def get_day_abbreviation(thisdate):
    """
    Given a date, return the pandas weekly frequency string like 'W-MON', 'W-TUE', etc.

    Parameters
    ----------
        thisdate : str or pd.Timestamp or datetime.date
            The input date.

    Returns
    -------
        str: Weekly frequency string like 'W-MON'

    """
    # Convert to Timestamp
    thisdate = pd.Timestamp(thisdate)
    # Get the day name abbreviation used in pandas week freq (e.g., MON, TUE, ...)
    weekday_abbr = f"W-{thisdate.strftime('%a').upper()}"  # e.g. 'MON'
    return weekday_abbr


# Identify the latest sampling day
def get_latest_sampling_day(day_given, sampling_day_abbrev):
    """
    Get the latest relevant weekday (to sample backwards from) looking back wrt the given date.

    Return current day if it is the latest day.
    """
    day_week_before = day_given - dt.timedelta(days=7)
    last_day = str(pd.date_range(start=day_week_before, end=day_given, freq=f"{sampling_day_abbrev}")[-1].date())
    return last_day


# Rename relevant columns and save
def rename_relevant_columns_and_save(*, input_path, listcols, col_names, output_path):
    """Read a csv, keep only the relevant columns, rename them, and save the result."""
    df = pd.read_csv(input_path)
    df = df[listcols]
    df.rename(columns=col_names, inplace=True)
    df.to_csv(output_path, index=False)
    return df


def check_existing_parsed_weather_data(parsed_folder, region_type="district"):
    """
    Check what parsed weather data files already exist.

    Parameters
    ----------
    parsed_folder : str or Path
        Path to the parsed netcdf folder.
    region_type : str, optional
        Region type ('district' or 'subdistrict'). Default is 'district'.

    Returns
    -------
    existing_months : set of tuples
        Set of (year, month) tuples for which we already have parsed data.
    """
    parsed_folder = Path(parsed_folder) / region_type
    existing_months = set()

    if not parsed_folder.exists():
        logger.info(f"Parsed folder does not exist: {parsed_folder}")
        return existing_months

    # Iterate through year folders
    for year_folder in parsed_folder.iterdir():
        if year_folder.is_dir() and year_folder.name.isdigit():
            year = int(year_folder.name)
            # Look for .csv files in the year folder
            for csv_file in year_folder.glob("*.csv"):
                # Extract month from filename pattern: YYYY_MM.csv or YYYY_MM_RegionName.csv
                filename = csv_file.stem
                parts = filename.split("_")
                if len(parts) >= 2 and parts[1].isdigit():
                    month = int(parts[1])
                    existing_months.add((year, month))

    logger.info(f"Found {len(existing_months)} existing parsed {region_type} weather data files")
    return existing_months


def get_missing_months_to_parse(start_date, end_date, existing_months):
    """
    Determine which months need to be parsed.

    Parameters
    ----------
    start_date : pd.Timestamp or datetime
        Start date for the data range.
    end_date : pd.Timestamp or datetime
        End date for the data range.
    existing_months : set of tuples
        Set of (year, month) tuples that are already parsed.

    Returns
    -------
    missing_months : list of tuples
        List of (year, month) tuples that need to be parsed.
    """
    start_date = pd.Timestamp(start_date)
    end_date = pd.Timestamp(end_date)

    # Generate all months in the range
    all_months = []
    current = start_date.replace(day=1)
    end = end_date.replace(day=1)

    while current <= end:
        all_months.append((current.year, current.month))
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)

    # Find missing months
    missing_months = [ym for ym in all_months if ym not in existing_months]

    logger.info(f"Missing {len(missing_months)} months to parse between {start_date.date()} and {end_date.date()}")
    return missing_months


def parse_and_extract_weather_data(
    root_dir: Path,
    geojson_folder,
    case_data_start_date=None,
    case_data_end_date=None,
    sampling_day=None,
    parse_district_level=True,
    parse_subdistrict_level=True,
):
    """
    Parse and extract weather data for the required date range.

    Parameters
    ----------
    root_dir : Path
        Root directory of the project.
    case_data_start_date : pd.Timestamp or datetime, optional
        Start date of case data (determines weather data start).
    case_data_end_date : pd.Timestamp or datetime, optional
        End date of case data (determines weather data end).

    Returns
    -------
    None
    """
    # Data parsing: Example usage
    source_folder = root_dir / "datasets/netcdf"
    destination_folder = root_dir / "datasets/parsednetcdf"
    w_params = ["t2m", "d2m", "tp"]

    selected_regions = []
    if parse_district_level:
        selected_regions.append("district")
    if parse_subdistrict_level:
        selected_regions.append("subdistrict")

    # Parse historical data if date range is provided
    if case_data_start_date is not None and case_data_end_date is not None:
        logger.info(f"Checking for historical weather data to parse from {case_data_start_date} to {case_data_end_date}")

        # Convert dates to (year, month) tuples
        start_date = pd.Timestamp(case_data_start_date)
        end_date = pd.Timestamp(case_data_end_date)

        # Check and parse district level historical data
        if parse_district_level:
            logger.info("Checking district level parsed data")
            existing_district = check_existing_parsed_weather_data(destination_folder, region_type="district")
            missing_district = get_missing_months_to_parse(start_date, end_date, existing_district)

            if missing_district:
                ym_i = missing_district[0]
                ym_j = missing_district[-1]
                logger.info(f"Parsing {len(missing_district)} missing months of district level data")
                parse_region_data_from_to(
                    ym_i=ym_i,
                    ym_j=ym_j,
                    source_folder=source_folder,
                    destination_folder=destination_folder,
                    geojson_folder=geojson_folder,
                    w_params=w_params,
                    region_type="district",
                )
            else:
                logger.info("All district level historical data already parsed")

        # Check and parse subdistrict level historical data
        if parse_subdistrict_level:
            logger.info("Checking subdistrict level parsed data")
            existing_subdistrict = check_existing_parsed_weather_data(destination_folder, region_type="subdistrict")
            missing_subdistrict = get_missing_months_to_parse(start_date, end_date, existing_subdistrict)

            if missing_subdistrict:
                ym_i = missing_subdistrict[0]
                ym_j = missing_subdistrict[-1]
                logger.info(f"Parsing {len(missing_subdistrict)} missing months of subdistrict level data")
                parse_region_data_from_to(
                    ym_i=ym_i,
                    ym_j=ym_j,
                    source_folder=source_folder,
                    destination_folder=destination_folder,
                    geojson_folder=geojson_folder,
                    w_params=w_params,
                    region_type="subdistrict",
                )
            else:
                logger.info("All subdistrict level historical data already parsed")

    # Parse recent months data (always run this to get latest data)
    logger.info("Parsing recent months weather data")
    # parse_region_data_recent(
    #     source_folder=source_folder,
    #     destination_folder=destination_folder,
    #     geojson_folder=geojson_folder,
    #     w_params=w_params,
    #     region_type="district",
    # )
    # parse_region_data_recent(
    #     source_folder=source_folder,
    #     destination_folder=destination_folder,
    #     geojson_folder=geojson_folder,
    #     w_params=w_params,
    #     region_type="subdistrict",
    # )

    # Daily data aggregation: Example usage
    w_params = ["t2m", "t2m", "t2m", "d2m", "tp"]
    operations = ["max", "min", "mean", "mean", "sum"]
    common_cols = ["region_id", "date", "name", "parent", "parent_name"]

    # All data
    if parse_district_level:
        # District
        source_folder = root_dir / "datasets/parsednetcdf/district"
        destination_folder = root_dir / "datasets/agg_daily/district"
        aggregate_daily_all(
            source_folder=source_folder,
            destination_folder=destination_folder,
            common_cols=common_cols,
            w_params=w_params,
            operations=operations,
        )
    if parse_subdistrict_level:
        # Subdistrict
        source_folder = root_dir / "datasets/parsednetcdf/subdistrict"
        destination_folder = root_dir / "datasets/agg_daily/subdistrict"
        aggregate_daily_all(
            source_folder=source_folder,
            destination_folder=destination_folder,
            common_cols=common_cols,
            w_params=w_params,
            operations=operations,
        )

    # N-days data aggregation: Example usage
    w_params = ["t2m_max", "t2m_min", "t2m_mean", "d2m_mean", "tp_sum"]
    operations = ["mean", "mean", "mean", "mean", "sum"]
    common_cols = ["region_id", "date", "name", "parent", "parent_name"]

    # All data
    if parse_district_level:
        # District
        source_folder = root_dir / "datasets/agg_daily/district"
        destination_folder = root_dir / "datasets/agg_Ndays/district"
        aggregate_Ndays_all(
            source_folder=source_folder,
            destination_folder=destination_folder,
            common_cols=common_cols,
            w_params=w_params,
            operations=operations,
        )
    if parse_subdistrict_level:
        # Subdistrict
        source_folder = root_dir / "datasets/agg_daily/subdistrict"
        destination_folder = root_dir / "datasets/agg_Ndays/subdistrict"
        aggregate_Ndays_all(
            source_folder=source_folder,
            destination_folder=destination_folder,
            common_cols=common_cols,
            w_params=w_params,
            operations=operations,
        )

    df_weather_region = {}

    for region in selected_regions:
        df_weather_region[region] = merge_aggregate_data(
            source_folder=f"datasets/agg_Ndays/{region}", destination_path=f"datasets/weather_{region}.csv"
        )

    # Get the abbreviation for the day on which we are doing predictions
    # sampling_day = get_day_abbreviation(thisdate=pd.Timestamp.today().normalize())

    # Sample the weather data
    # Identify the latest sampling day
    max_date_weather = pd.Timestamp(max(df_weather_region[selected_regions[0]]["date"]))
    latest_day = get_latest_sampling_day(day_given=max_date_weather, sampling_day_abbrev=sampling_day)
    logger.info(f"sampling_day: {sampling_day}")
    logger.info(f"latest_day: {latest_day}")

    df_weather_region_sample = {}

    for region in selected_regions:
        df_weather_region_sample[region] = sample_data(
            filepath=root_dir / f"datasets/weather_{region}.csv",
            sample_filepath=root_dir / f"datasets/weather_{region}_sampled.csv",
            end_date=latest_day,
            sample_from="end",
            sampling_rate=7,
            sampling_day=sampling_day,
        )

    # Example: Rename relevant columns and save
    listcols = ["region_id", "date", "name", "t2m_mean", "d2m_mean", "tp_sum"]
    col_names = {
        "date": "metadata.primaryDate",
        "t2m_mean": "2mTemperature",
        "d2m_mean": "2mDewpointTemperature",
        "tp_sum": "totalPrecipitation",
    }

    if parse_district_level:
        col_names["region_id"] = "location.admin2.ID"
        col_names["name"] = "location.admin2.name"

        rename_relevant_columns_and_save(
            input_path=root_dir / "datasets/weather_district_sampled.csv",
            listcols=listcols,
            col_names=col_names,
            output_path=root_dir / "datasets/processed_aggregated_era5_Karnataka_District.csv",
        )
    if parse_subdistrict_level:
        col_names["region_id"] = "location.admin3.ID"
        col_names["name"] = "location.admin3.name"

        rename_relevant_columns_and_save(
            input_path=root_dir / "datasets/weather_subdistrict_sampled.csv",
            listcols=listcols,
            col_names=col_names,
            output_path=root_dir / "datasets/processed_aggregated_era5_Karnataka_Subdistrict.csv",
        )


if __name__ == "__main__":
    path = Path(".")

    logging.basicConfig(filename="./logger.log", format="%(asctime)s %(message)s", filemode="w")

    parse_and_extract_weather_data(root_dir=path)

    # Setting the threshold of logger to DEBUG
    logger.setLevel(logging.DEBUG)

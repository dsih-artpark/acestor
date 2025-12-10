# -*- coding: utf-8 -*-
"""
Created on Mon Sep 30 16:09:57 2024.

@author: TarunK
"""

import numpy as np
import geopandas as gpd
import pandas as pd
import cdsapi
import xarray as xr
from pathlib import Path
import os
import logging
import zipfile
import magic
import datetime as dt

logger = logging.getLogger("download_weather_data")


# %% Functions for computing region bounds
def return_region_bounds(*, union_polygon):
    """
    Read a union polygon.

    Compute and return the bounds for the geometry.

    Parameters
    ----------
    union_polygon : shapely.geometry.polygon.Polygon
        The union of all the polygons in a gopandas file

    Returns
    -------
    bound_n, bound_w, bound_s, bound_e : float
        Approximate region bounds (maximum and minimum latitudes and longitudes) for a polygon

    """
    bound_w0, bound_s0, bound_e0, bound_n0 = union_polygon.bounds
    # Ensure the following for bound_n, bound_w, bound_s, and bound_e:
    # 1. They are some multiple of 0.25
    # 2. They are sufficiently away from the region boundary
    #    Hence, 0.25 is added or subtracted
    bound_w = float(np.floor(bound_w0 * 4) / 4.0) - 0.25
    bound_s = float(np.floor(bound_s0 * 4) / 4.0) - 0.25
    bound_e = float(np.ceil(bound_e0 * 4) / 4.0) + 0.25
    bound_n = float(np.ceil(bound_n0 * 4) / 4.0) + 0.25
    return bound_n, bound_w, bound_s, bound_e


def return_region_bounds_multiple_polygons(*, geojson_folder):
    """
    Read geojson files in a folder.

    Compute and return the bounds for the union of the polygons corresponding to the geojson files.

    Parameters
    ----------
    geojson_folder : str or Path
        The path to the folder containing all the geojson files of interest.

    Returns
    -------
    region_bounds : tuple (of floats)
        Approximate region bounds (maximum and minimum latitudes and longitudes: bound_n, bound_w, bound_s, bound_e)
        for a composite polygon.

    """
    geojson_folder = Path(geojson_folder)
    listfiles = os.listdir(geojson_folder)
    gdfs = [gpd.read_file(geojson_folder / thisfile) for thisfile in listfiles]
    region_gdf = gpd.GeoDataFrame(pd.concat(gdfs, ignore_index=True))
    region_gdf = region_gdf.dissolve(by="parent_name").reset_index()
    region_gdf = region_gdf.to_crs(epsg=4326)
    # Get region bounds
    region_bounds = return_region_bounds(union_polygon=region_gdf.geometry.union_all())
    return region_bounds


# %% Other functions
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


# %% Define the functions to download data, read the file(s) and parse them
def get_data(*, region_bounds, variables_of_interest, ym, output_filepath):
    """
    Download data (the variables of interest) for a region, for a given year and month; and saves it.

    Parameters
    ----------
    region_bounds : tuple
        A tuple (of floats) containing bound_n, bound_w, bound_s, bound_e.
    variables_of_interest : list
        The list of (strings of) variables of interest.
    ym : tuple (if int)
        The (year, month) for which the data is requested.
    output_filepath : str or Path
        The path at which the downloaded file should be saved.

    Returns
    -------
    None.

    """
    rb = region_bounds
    bound_n, bound_w, bound_s, bound_e = rb[0], rb[1], rb[2], rb[3]
    year = ym[0]
    month = ym[1]
    dataset = "reanalysis-era5-single-levels"
    request = {
        "product_type": ["reanalysis"],
        "variable": variables_of_interest,
        "year": [f"{year}"],
        "month": [f"{month:02}"],
        "day": [f"{val:02}" for val in range(1, 32)],
        "time": [f"{val:02}:00" for val in range(0, 24)],
        "data_format": "netcdf",
        "download_format": "unarchived",
        # "area": [23.509, 71.839, 21.97, 72.843],
        "area": [bound_n, bound_w, bound_s, bound_e],
    }
    client = cdsapi.Client()
    try:
        client.retrieve(dataset, request, output_filepath)
        logging.info(f"{dataset} ({year}, {month})| Downloaded at: {output_filepath}")
        logging.info("*" * 40)
        return_message = "Download successful!"
    except Exception as e:
        # Handle specific exceptions and print an informative message
        error_message = str(e)
        if "None of the data you have requested is available" in error_message or "not available" in error_message:
            logging.info(f"Data for {year}-{month:02d} is not available. Skipping this month: {e}")
            return_message = "No data available"
        else:
            logging.info(f"An unexpected error occurred for {year}-{month:02d}: {error_message}")
            return_message = "Error in data request"
    return return_message


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


def get_data_from_to(*, region_bounds, variables_of_interest, ym_i, ym_j, base_path, region_name=None):
    """
    Download data for a region between i-th (year, month) and j-th (year, month), both included.

    Parameters
    ----------
    region_bounds : tuple
        A tuple (of floats) containing bound_n, bound_w, bound_s, bound_e.
    variables_of_interest : list (of strings)
        The variables of interest within the dataset.
    ym_i : tuple (of integers)
        The (year, month) from to which the data is to be downloaded.
    ym_j : tuple (of integers)
        The (year, month) up to which the data is to be downloaded.
    base_path: str or Path
        The base path of the folder in which the downloaded file(s) will be saved.
    region_name: str
        The region for which the data is to be downloaded.

    Returns
    -------
    return_message: str
        The message that tells whether the data download was successful or not, and if there was some issue.

    """
    years = np.arange(ym_i[0], ym_j[0] + 1)
    months = np.arange(1, 13)
    yearmonth = [(y, m) for y in years for m in months if (([y, m] >= [ym_i[0], ym_i[1]]) and ([y, m] <= [ym_j[0], ym_j[1]]))]
    # Iterate over all the (year, month) pairsand download data
    for ym in yearmonth:
        year = ym[0]
        month = ym[1]
        # Specify the folder where you want to save the data
        folder_path = Path(f"{base_path}/{year}")
        # Create the folder if it doesn't exist
        folder_path.mkdir(parents=True, exist_ok=True)
        # Specify the filename
        if region_name is not None:
            output_filepath = Path(f"{base_path}/{year}/{year}_{month:02}_{region_name}.nc")
        else:
            output_filepath = Path(f"{base_path}/{year}/{year}_{month:02}.nc")
        str_file_name = str(output_filepath).replace("\\", "/").split("/")[-1].split(".")[0]
        del_file_with_name(folder_path=base_path, file_name_string=str_file_name)
        return_message = get_data(
            region_bounds=region_bounds, variables_of_interest=variables_of_interest, ym=ym, output_filepath=output_filepath
        )
        if return_message == "Download successful!":
            df, filetype = parse_data(input_file=output_filepath, save_as_nc=False)
            # output_filepath.unlink()
            logger.info(f"INFO: File {year}_{month:02}: Columns: {list(df.columns)}")
            output_filepath = Path(str(output_filepath).replace(".nc", f".{filetype}"))
            std_data(filepath=output_filepath)
    return return_message


def get_data_recent(*, region_bounds, variables_of_interest, base_path, region_name=None):
    """
    Download data for a region for the recent (current and immediately previous) months.

    Parameters
    ----------
    region_bounds : tuple
        A tuple (of floats) containing bound_n, bound_w, bound_s, bound_e.
    variables_of_interest : list (of strings)
        The variables of interest within the dataset.
    base_path: str or Path
        The base path of the folder in which the downloaded file(s) will be saved.
    region_name: str
        The region for which the data is to be downloaded.

    Returns
    -------
    return_message: str
        The message that tells whether the data download was successful or not, and if there was some issue.

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
    return_message = get_data_from_to(
        region_bounds=region_bounds,
        variables_of_interest=variables_of_interest,
        ym_i=ym_i,
        ym_j=ym_j,
        base_path=base_path,
        region_name=region_name,
    )
    return return_message


def check_existing_weather_data(netcdf_folder):
    """
    Check what weather data files already exist in the datasets/netcdf folder.

    Parameters
    ----------
    netcdf_folder : str or Path
        Path to the netcdf folder containing weather data organized by year.

    Returns
    -------
    existing_months : set of tuples
        Set of (year, month) tuples for which we already have data.
    """
    netcdf_folder = Path(netcdf_folder)
    existing_months = set()

    if not netcdf_folder.exists():
        logger.info(f"NetCDF folder does not exist: {netcdf_folder}")
        return existing_months

    # Iterate through year folders
    for year_folder in netcdf_folder.iterdir():
        if year_folder.is_dir() and year_folder.name.isdigit():
            year = int(year_folder.name)
            # Look for .nc and .zip files in the year folder
            for weather_file in year_folder.glob("*"):
                if weather_file.suffix in [".nc", ".zip"]:
                    # Extract month from filename pattern: YYYY_MM.nc or YYYY_MM_RegionName.nc
                    filename = weather_file.stem  # filename without extension
                    parts = filename.split("_")
                    if len(parts) >= 2 and parts[1].isdigit():
                        month = int(parts[1])
                        existing_months.add((year, month))

    logger.info(f"Found {len(existing_months)} existing weather data files")
    return existing_months


def get_missing_months(start_date, end_date, existing_months):
    """
    Determine which months are missing between start_date and end_date.

    Parameters
    ----------
    start_date : pd.Timestamp or datetime
        Start date for the data range.
    end_date : pd.Timestamp or datetime
        End date for the data range.
    existing_months : set of tuples
        Set of (year, month) tuples that already exist.

    Returns
    -------
    missing_months : list of tuples
        List of (year, month) tuples that need to be downloaded.
    """
    # Convert to pandas timestamps for easier date handling
    start_date = pd.Timestamp(start_date)
    end_date = pd.Timestamp(end_date)

    # Generate all months in the range
    all_months = []
    current = start_date.replace(day=1)
    end = end_date.replace(day=1)

    while current <= end:
        all_months.append((current.year, current.month))
        # Move to next month
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)

    # Find missing months
    missing_months = [ym for ym in all_months if ym not in existing_months]

    logger.info(f"Missing {len(missing_months)} months of weather data between {start_date.date()} and {end_date.date()}")
    return missing_months


def download_weather_data(root_dir, case_data_start_date, case_data_end_date, region_name, geojson_folder_path):
    """
    Download weather data for the required date range.

    Only downloads data for months that are missing in the datasets/netcdf folder.

    Parameters
    ----------
    root_dir : str or Path
        Root directory of the project.
    case_data_start_date : pd.Timestamp or datetime
        Start date of case data (determines weather data start).
    case_data_end_date : pd.Timestamp or datetime
        End date of case data (determines weather data end).
    region_name : str
        Name of the region (e.g., 'Karnataka', 'TamilNadu').
    geojson_folder_path : str or Path
        Path to the folder containing geojson files for the region.

    Returns
    -------
    return_message : str
        Status message about the download operation.
    """
    # Specify the region bounds
    region_bounds = return_region_bounds_multiple_polygons(geojson_folder=geojson_folder_path)

    # Download data (variables of interest) for the year and months of interest
    variables_of_interest = ["2m_temperature", "2m_dewpoint_temperature", "total_precipitation"]
    base_path = root_dir / "datasets/netcdf"

    # Check if we have historical weather data
    existing_months = check_existing_weather_data(netcdf_folder=base_path)

    # Download weather data for the months we don't have historical data
    missing_months = get_missing_months(start_date=case_data_start_date, end_date=case_data_end_date, existing_months=existing_months)

    if missing_months:
        logger.info(f"Downloading {len(missing_months)} missing months of weather data for {region_name}")
        # Group consecutive months for efficient batch downloading
        # Download from first missing month to last missing month
        ym_i = missing_months[0]  # (year, month) tuple
        ym_j = missing_months[-1]  # (year, month) tuple

        return_message = get_data_from_to(
            region_bounds=region_bounds,
            variables_of_interest=variables_of_interest,
            ym_i=ym_i,
            ym_j=ym_j,
            base_path=base_path,
            region_name=region_name,
        )
    else:
        logger.info("All required weather data already exists")
        return_message = "All data already available"

    # Delete the recent month files, because we want to download the latest ones for the same months
    # This ensures we get the most up-to-date data for recent months
    delete_recent_files(base_path=base_path)

    # Download recent months' data (current and previous month)
    # This ensures we always have the latest data for recent months
    logger.info(f"Downloading recent months' data for {region_name}")
    recent_message = get_data_recent(
        region_bounds=region_bounds, variables_of_interest=variables_of_interest, base_path=base_path, region_name=region_name
    )

    return return_message


if __name__ == "__main__":
    # %% Specify the path and the regions for which the data will be dowloaded
    root_dir = Path(".")

    logging.basicConfig(filename=root_dir / "logs/download_weather_data.log", format="%(asctime)s %(message)s", filemode="w")
    # Creating an object
    logger = logging.getLogger()

    # Setting the threshold of logger to DEBUG
    logger.setLevel(logging.DEBUG)

    download_weather_data(root_dir=root_dir)

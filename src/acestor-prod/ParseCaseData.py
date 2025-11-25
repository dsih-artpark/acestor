# -*- coding: utf-8 -*-
"""
Created on Fri Mar 14 11:35:14 2025

@author: TarunK
"""

import numpy as np
import pandas as pd
import os
import logging
import datetime as dt
import yaml

from typing import Literal
from pathlib import Path
from datetime import datetime


logger = logging.getLogger("parse-case-data")


# %% Identify columns of interest
def gen_col_dict(*, region_type="district"):
    """
    Generate the data columns (and mapping) depending on the type of rgion_id.

    Parameters
    ----------
    region_type : str, optional
        The type of the regions of interest (state, district, subdistrict, ulb, zone, village, or ward).
        The default is 'district'.

    Returns
    -------
    cols : dict
        The dictionary that maps linelist column names to those that are amenable to the data pipeline.

    """
    cols = {"metadata.primaryDate": "date"}
    match region_type:
        case "state":
            cols["location.admin1.ID"] = "region_id"
        case "ut":
            cols["location.admin1.ID"] = "region_id"
        case "district":
            cols["location.admin2.ID"] = "region_id"
        case "subdistrict":
            cols["location.admin3.ID"] = "region_id"
        case "ulb":
            cols["location.admin3.ID"] = "region_id"
        case "zone":
            cols["location.admin4.ID"] = "region_id"
        case "village":
            cols["location.admin5.ID"] = "region_id"
        case "ward":
            cols["location.admin5.ID"] = "region_id"
    return cols


# %% Fetch file list
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


# %% Merge all case data and filter
def merge_filter_data(*, folder_path, cols_of_interest, output_file_path):
    """
    Merge all the raw data over the columns of interest.

    Fetch the data only for the time period of interest.
    Save the filtered data as a .csv file.

    Parameters
    ----------
    folder_path : str or Path
        The folder from which we want to fetch the raw data for merging.
    cols_of_interest : list (of str)
        The columns of interest over which we want to merge all the data.
    date_i : pandas Timestamp
        The date from which we want to use the data.
    date_j : pandas Timestamp
        The date up to which we want to use the data.
    output_file_path : str or Path
        The path for the output file.

    Returns
    -------
    merged_df : pandas dataframe
        The dataframe containing all the data merged in one.

    """
    logger.info("merging and filtering case data")

    folder_path = Path(folder_path)
    output_file_path = Path(output_file_path)

    os.makedirs(output_file_path.parent, exist_ok=True)

    file_list = get_datafile_list(source_path=folder_path, filetypes=[".csv"])

    logger.info(f"{len(file_list)} files found")

    list_df = [pd.read_csv(thisfile)[cols_of_interest] for thisfile in file_list]
    merged_df = pd.concat(list_df).reset_index(drop=True)
    merged_df["metadata.primaryDate"] = merged_df["metadata.primaryDate"].str.replace("Z", "", regex=False)
    merged_df["metadata.primaryDate"] = pd.to_datetime(merged_df["metadata.primaryDate"], errors="coerce").dt.tz_localize(None)
    merged_df["metadata.primaryDate"] = pd.to_datetime(merged_df["metadata.primaryDate"]).dt.date.astype("datetime64[ns]")

    merged_df.to_csv(output_file_path, index=False)

    return merged_df


# %% Aggregate cases for each day for all regions
def aggregate_daily(*, folder_path, cols, disease, output_file_path):
    """
    Aggregate case data for each region on a daily basis.

    Parameters
    ----------
    folder_path : str or Path
        The path to the folder containing the clean and filtered linelist.
    cols : list (of str)
        List of columns of interest.
    disease : str
        Disease of interest. This argument helps filtering out any blank or irrelevant rows.
    output_file_path : str or Path
        Path to the file containing aggregate data.

    Returns
    -------
    df_agg : pandas dataframe
        The dataframe containing daily aggregated cases for each region of interest.

    """

    def fill_missing_dates(group: pd.DataFrame):
        """Generate full date range for a district and fill missing cases with 0."""
        full_range = pd.date_range(start=group["date"].min(), end=group["date"].max())
        group = group.set_index("date").reindex(full_range).reset_index()
        group.rename(columns={"index": "date"}, inplace=True)
        return group

    folder_path = Path(folder_path)
    output_file_path = Path(output_file_path)
    if not os.path.exists(output_file_path.parent):
        os.mkdir(output_file_path.parent)
    cols_of_interest = list(cols.keys())
    list_df = [pd.read_csv(folder_path / thisfile, low_memory=False)[cols_of_interest] for thisfile in os.listdir(folder_path)]
    df = pd.concat(list_df)
    df.rename(columns=cols, inplace=True)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["date"] = df["date"].dt.date.astype("datetime64[ns]")
    df = df.dropna()
    df = df.sort_values(["region_id", "date"], ascending=[True, True]).reset_index(drop=True)
    # Aggregate over all region_id and date. NOTE: Data for all (continuous) dates may not be available.
    df_agg_sparse = df.groupby(["region_id", "date"]).value_counts().reset_index(name="case")
    # Fill in missing dates. Fill in missing-date cases with zero.
    # 1. Fill missing dates
    df_agg = df_agg_sparse.groupby("region_id", group_keys=False)[["region_id", "date", "case"]].apply(fill_missing_dates)
    # 2. Fill zero for cases
    df_agg["case"] = df_agg["case"].fillna(0)
    # 3. Forward fill other column values
    df_agg["region_id"] = df_agg["region_id"].ffill()
    df_agg = df_agg[["region_id", "date", "case"]].reset_index(drop=True)
    df_agg.to_csv(output_file_path, index=False)
    return df_agg


# %% Aggregate cases over N days for all regions
def filter_and_rolling_aggregate_Ndays(*, input_file_path, output_file_path, run_date, N=7):
    """
    Aggregate the data in a rolling manner (previous N data points including the given day) for each day.

    Parameters
    ----------
    input_file_path: str or Path
        The path for the input file contining daily cases for each region.
    output_file_path: str or Path
        The path for the output file contining rolling aggregate cases over N-days.
    N : int, optional
        The number of days for which we want the rolling sum values. The default is 7.

    Returns
    -------
    pandas datafrane
        The dataframe containing rolling N-day aggregated values.

    """

    def rolling_fn(group, common_cols=["region_id", "date"]):
        group = group.sort_values("date").reset_index()  # Ensure data is sorted by date
        group = group.set_index("date")  # Set date as index for rolling operation
        # Perform rolling aggregation on numeric columns
        rolling_df = group["case"].rolling(f"{N - 1}D", closed="both").agg({"case": "sum"}).reset_index()
        rolling_df = rolling_df.merge(group.reset_index()[common_cols], on="date", how="left")
        return rolling_df

    input_file_path = Path(input_file_path)
    output_file_path = Path(output_file_path)
    df = pd.read_csv(input_file_path)
    df["date"] = pd.to_datetime(df["date"], format="%d/%m/%Y")

    # filter out data before run date

    df = df[df["date"] < run_date]

    listcols = ["region_id", "date", "case"]
    df_aggN = df.groupby("region_id")[listcols].apply(rolling_fn).reset_index(drop=True)
    df_aggN = df_aggN[listcols]
    df_aggN.to_csv(output_file_path, index=False)
    return df_aggN


# %% Sample data
def sample_data(
    *,
    filepath,
    sample_filepath,
    start_date: pd.Timestamp | None = None,
    end_date: pd.Timestamp | None = None,
    sample_from: Literal["beginning", "end"] = "end",
    sampling_rate=7,
):
    """
    Sample the data from a file at a sampling rate, save the resultant dataframe, and also return it.

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
        The rate at which we want to sample the data. The default is 7.

    Returns
    -------
    df_sample : pandas dataframe
        The sample file.

    """
    filepath = Path(filepath)
    sample_filepath = Path(sample_filepath)
    if sample_from not in {"beginning", "end"}:
        raise ValueError(f"'sample_from' must be 'beginning' or 'end', got '{sample_from}'")
    N = sampling_rate
    df = pd.read_csv(filepath)
    list_dates = list(df["date"].unique())
    list_dates.sort()
    if start_date is not None:
        start_date = pd.Timestamp(start_date).strftime("%Y-%m-%d")
        list_dates = [val for val in list_dates if (val >= start_date)]
    if end_date is not None:
        end_date = pd.Timestamp(end_date).strftime("%Y-%m-%d")
        list_dates = [val for val in list_dates if (val <= end_date)]
    if sample_from == "end":
        sample_dates = list_dates[::-N]
    elif sample_from == "beginning":
        sample_dates = list_dates[::N]
    df_sample = df[df["date"].isin(sample_dates)].reset_index(drop=True)
    df_sample.to_csv(sample_filepath)
    return df_sample


# %% Get day abbreviation. This determines sampling of case and weather data
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


# %% Identify the latest sampling day
def get_latest_sampling_day(day_given, sampling_day_abbrev):
    """
    Get the latest relevant weekday (to sample backwards from) looking back wrt the given date.

    Return current day if it is the latest day.
    """
    day_week_before = day_given - dt.timedelta(days=7)
    last_day = str(pd.date_range(start=day_week_before, end=day_given, freq=f"{sampling_day_abbrev}")[-1].date())
    return last_day


# %% Rename relevant columns and save
def rename_relevant_columns_and_save(*, input_path, listcols, col_names, output_path):
    """Read a csv, keep only the relevant columns, rename them, and save the result."""
    df = pd.read_csv(input_path, index_col=0)
    df = df[listcols]
    df.rename(columns=col_names, inplace=True)
    df.to_csv(output_path, index=False)
    return df


def filter_last_year_data(*, input_path, output_path):
    """
    Filter data to keep only the last year for debugging purposes.

    Parameters
    ----------
    input_path : str or Path
        Path to the input CSV file.
    output_path : str or Path
        Path to save the filtered CSV file.

    Returns
    -------
    df : pandas DataFrame
        The filtered dataframe containing only last year's data.
    """
    df = pd.read_csv(input_path)
    df["date"] = pd.to_datetime(df["date"], format="mixed")

    # Get data from last year only
    max_date = df["date"].max()
    one_year_ago = max_date - pd.DateOffset(years=1)

    df_filtered = df[df["date"] >= one_year_ago].reset_index(drop=True)

    logger.info(f"Filtered data from {one_year_ago.date()} to {max_date.date()} ({len(df_filtered)} records)")

    df_filtered.to_csv(output_path, index=False)
    return df_filtered


def parse_linelist_to_no_of_cases(root_dir, raw_linelist_path, parse_district_level=True, parse_subdistrict_level=True):
    root_dir = Path(root_dir)

    # Define fields of interest (common for both district and subdistrict)
    fields_of_interest = [
        "metadata.primaryDate",
        "location.admin1.ID",
        "location.admin2.ID",
        "location.admin3.ID",
        "location.admin5.ID",
    ]

    if parse_district_level:
        os.makedirs(root_dir / "datasets/filtered_linelist_data/district", exist_ok=True)

        # Process DISTRICT level
        region_type = "district"
        logger.info(f"Processing {region_type} level data")

        cols = gen_col_dict(region_type=region_type)

        # Merging case data
        merged_df = merge_filter_data(
            folder_path=raw_linelist_path,
            cols_of_interest=fields_of_interest,
            output_file_path=root_dir / f"datasets/filtered_linelist_data/{region_type}/filtered_linelist.csv",
        )

        # Aggregate cases of each day for all regions
        df_region_daily = aggregate_daily(
            folder_path=root_dir / f"datasets/filtered_linelist_data/{region_type}",
            cols=cols,
            disease="Dengue",
            output_file_path=root_dir / f"datasets/cases_{region_type}_daily.csv",
        )
    if parse_subdistrict_level:
        os.makedirs(root_dir / "datasets/filtered_linelist_data/subdistrict", exist_ok=True)

        # Process SUBDISTRICT level
        region_type = "subdistrict"
        logger.info(f"Processing {region_type} level data")

        cols = gen_col_dict(region_type=region_type)

        # Merging case data
        merged_df_KA = merge_filter_data(
            folder_path=raw_linelist_path,
            cols_of_interest=fields_of_interest,
            output_file_path=root_dir / f"datasets/filtered_linelist_data/{region_type}/KA_linelist.csv",
        )

        merged_df_IHIP = merge_filter_data(
            folder_path=root_dir / "datasets/raw_linelist_data/IHIP_linelist",
            cols_of_interest=fields_of_interest,
            output_file_path=root_dir / f"datasets/filtered_linelist_data/{region_type}/IHIP_linelist.csv",
        )

        # Aggregate cases of each day for all regions
        df_region_daily = aggregate_daily(
            folder_path=root_dir / f"datasets/filtered_linelist_data/{region_type}",
            cols=cols,
            disease="Dengue",
            output_file_path=root_dir / f"datasets/cases_{region_type}_daily.csv",
        )


def aggregate_and_sample_case_data(
    root_dir,
    run_date,
    debug=False,
    parse_district_level=True,
    parse_subdistrict_level=True,
):
    """
    Main function to parse and process case data.

    This function orchestrates the complete case data parsing pipeline:
    1. Merge and filter raw data from multiple sources
    2. Aggregate daily cases
    3. Create rolling aggregates
    4. Sample data at specified intervals
    5. Rename columns for final output

    Parameters
    ----------
    root_dir : str or Path
        Root directory for the pipeline (from config).
    debug : bool, optional
        If True, only process data from the last year for testing purposes. Default is False.
    """

    logger.info(f"Using root directory: {root_dir}")

    if debug:
        logger.info("DEBUG MODE: Processing only last year of case data")

    # Get the abbreviation for the day on which we are doing predictions
    sampling_day = get_day_abbreviation(thisdate=run_date)

    # Filter out data prior to run date

    logger.info(f"sampling day is {sampling_day}")

    if parse_district_level:
        region_type = "district"

        # Filter to last year only if debug mode
        if debug:
            logger.info(f"Filtering {region_type} daily data to last year only (debug mode)")
            df_region_daily = filter_last_year_data(
                input_path=root_dir / f"datasets/cases_{region_type}_daily.csv",
                output_path=root_dir / f"datasets/cases_{region_type}_daily.csv",
            )

        # Aggregate cases over N days for all regions
        df_region_Ndays = filter_and_rolling_aggregate_Ndays(
            input_file_path=root_dir / f"datasets/cases_{region_type}_daily.csv",
            output_file_path=root_dir / f"datasets/cases_{region_type}.csv",
            run_date=run_date,
            N=7,
        )

        # Identify the latest sampling day
        max_date_cases = pd.Timestamp(max(df_region_Ndays["date"]))
        latest_day = get_latest_sampling_day(day_given=max_date_cases, sampling_day_abbrev=sampling_day)

        # Sample the data
        df_case_region_sample = sample_data(
            filepath=root_dir / f"datasets/cases_{region_type}.csv",
            sample_filepath=root_dir / f"datasets/cases_{region_type}_sampled.csv",
            end_date=latest_day,
            sample_from="end",
            sampling_rate=7,
        )

    if parse_subdistrict_level:
        # Process SUBDISTRICT level
        region_type = "subdistrict"
        logger.info(f"Processing {region_type} level data")

        # Filter to last year only if debug mode
        if debug:
            logger.info(f"Filtering {region_type} daily data to last year only (debug mode)")
            df_region_daily = filter_last_year_data(
                input_path=root_dir / f"datasets/cases_{region_type}_daily.csv",
                output_path=root_dir / f"datasets/cases_{region_type}_daily.csv",
            )

        # Aggregate cases over N days for all regions
        df_region_Ndays = filter_and_rolling_aggregate_Ndays(
            input_file_path=root_dir / f"datasets/cases_{region_type}_daily.csv",
            output_file_path=root_dir / f"datasets/cases_{region_type}.csv",
            run_date=run_date,
            N=7,
        )

        # Identify the latest sampling day
        max_date_cases = pd.Timestamp(max(df_region_Ndays["date"]))
        latest_day = get_latest_sampling_day(day_given=max_date_cases, sampling_day_abbrev=sampling_day)

        # Sample the data
        df_case_region_sample = sample_data(
            filepath=root_dir / f"datasets/cases_{region_type}.csv",
            sample_filepath=root_dir / f"datasets/cases_{region_type}_sampled.csv",
            end_date=latest_day,
            sample_from="end",
            sampling_rate=7,
        )

    # Rename relevant columns and save
    logger.info("Renaming columns for final output")
    listcols = ["region_id", "date", "case"]
    col_names = {"date": "metadata.primaryDate"}

    if parse_district_level:
        col_names["region_id"] = "location.admin2.ID"
        df_dist = rename_relevant_columns_and_save(
            input_path=root_dir / "datasets/cases_district_sampled.csv",
            listcols=listcols,
            col_names=col_names,
            output_path=root_dir / "datasets/cases_district_sampled.csv",
        )

    if parse_subdistrict_level:
        col_names["region_id"] = "location.admin3.ID"
        df_subdist = rename_relevant_columns_and_save(
            input_path=root_dir / "datasets/cases_subdistrict_sampled.csv",
            listcols=listcols,
            col_names=col_names,
            output_path=root_dir / "datasets/cases_subdistrict_sampled.csv",
        )

    logger.info("Case data parsing completed successfully")

    case_start_date = df_dist["metadata.primaryDate"].min()
    case_end_date = df_dist["metadata.primaryDate"].max()

    return sampling_day, case_start_date, case_end_date


if __name__ == "__main__":
    # Load config
    with open("config/dengue_pipeline.yaml", "r") as f:
        config = yaml.safe_load(f)

    root_dir = Path(config["root_dir"])

    # Set up logging for standalone execution
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(root_dir / f"logs/ParseCaseData_{datetime.now().strftime('%Y%m%d%H%M%S')}.log"),
            logging.StreamHandler(),  # This will print to console
        ],
    )

    # Run the main parsing function
    # run_parse_case_data_and_return_sampling_day_start_date_end_date(root_dir=root_dir)
    print("Cant run this as main for now!! Sorry!!")

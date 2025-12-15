# -*- coding: utf-8 -*-
"""
Created on Mon Jul 14 16:26:08 2025

@author: Tarun
"""

import numpy as np
import pandas as pd
from copy import deepcopy
from pathlib import Path
import os
import logging
import datetime as dt
import boto3
from tqdm import tqdm
import sys
from typing import Literal
import pickle

logger = logging.getLogger("generate-thresholds")


# %% Align dates for N-day rolling average values for all regions by filling in zeroes for missing dates.
def align_dates_all_regions(*, input_file_path):
    """
    Generate (date aligned across regions) rolling aggregate cases over N-days.

    Parameters
    ----------
    input_file_path: str or Path
        The path for the input file contining rolling aggregate cases over N-days.
        However, the dates for all regions are not aligned.

    Returns
    -------
    df_align : pandas dataframe
        The dataframe containing date aligned N-day rolling average values for all regions.

    """
    input_file_path = Path(input_file_path)
    # output_file_path = Path(output_file_path)
    df = pd.read_csv(input_file_path)
    df["date"] = pd.to_datetime(df["date"])
    full_range = pd.date_range(start=df["date"].min(), end=df["date"].max())

    def fill_missing_dates(group: pd.DataFrame, full_range=full_range):
        """Generate full date range for a district and fill missing cases with 0."""
        group = group.set_index("date").reindex(full_range).reset_index()
        group.rename(columns={"index": "date"}, inplace=True)
        group["case"] = group["case"].fillna(0)
        group["region_id"] = group["region_id"].ffill()
        group["region_id"] = group["region_id"].bfill()
        return group

    listcols = ["region_id", "date", "case"]
    df_align = df.groupby("region_id")[listcols].apply(fill_missing_dates).reset_index(drop=True)
    df_align = df_align[listcols]
    # df_align.to_csv(output_file_path, index=False)
    return df_align


# %% Generate thresholds
# Method 1: Previous N-weeks
def prev_Nweeks_threshold_params(*, df, output_path, N=4, k=7):
    """
    Return the parameters to generate previous N-week thresholds.

    Parameters
    ----------
    df : pd.DataFrame
        The region wise list of cases.
    output_path : str or Path
        The path to the file containing the threshold values.
    N : int, optional
        The number of intervals(weeks) of interest. The default is 4.
    k : int, optional
        The length of an interval (in days). The default is 7.

    Raises
    ------
    ValueError
        If the stat is not computable.

    Returns
    -------
    df_thresh : pandas dataframe
        Dataframe of threshold paramters (mean and std dev) through which we can generate threshold levels.

    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)  # Ensure the parent directory exists

    def process_group(group, value_col, stat, N=4, k=7, min_count=1, closed="left"):
        group = group.reset_index(drop=True)
        dates = group["date"]
        values = group[value_col]
        # Build a date: value mapping for the current group
        value_lookup = dict(zip(dates, values))
        # For each current date, compute target dates: curr - k*i for i = 1 to N
        if closed == "left":
            target_dates = pd.DataFrame({f"day_{i}": dates - pd.Timedelta(days=k * i) for i in range(0, N)})
        else:
            target_dates = pd.DataFrame({f"day_{i}": dates - pd.Timedelta(days=k * i) for i in range(1, N + 1)})
        # Map each target date to the value in value_lookup
        target_values = target_dates.map(lambda d: value_lookup.get(d, np.nan))
        # Count non-NaN values in each row
        valid_counts = target_values.notna().sum(axis=1)
        # Compute stat only where count >= min_count
        if stat == "mean":
            computed_stat = target_values.mean(axis=1, skipna=True)
        elif stat == "std":
            computed_stat = target_values.std(axis=1, skipna=True)
        elif callable(stat):
            computed_stat = target_values.apply(lambda row: stat(row.dropna()), axis=1)
        else:
            raise ValueError(f"Unsupported stat: {stat}")
            # Set stat to NaN where there are too few valid values
        computed_stat[valid_counts < min_count] = np.nan
        return computed_stat

    # For each region: At week N, compute mean and std of previous 4 weeks (N-1 to N-4)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["region_id", "date"]).copy()

    thresh = []
    for region_id, group in df.groupby("region_id"):
        group = group.reset_index(drop=True)
        group[f"Mean_N{N}week_k{k}days"] = process_group(group=group, value_col="case", stat="mean", closed="left")
        group["Mean"] = process_group(group=group, value_col=f"Mean_N{N}week_k{k}days", stat="mean", N=3, closed=None)
        group["StdDev"] = process_group(group=group, value_col=f"Mean_N{N}week_k{k}days", stat="std", closed=None)
        thresh.append(group)
    df_thresh = pd.concat(thresh, ignore_index=True)[["region_id", "date", "case", f"Mean_N{N}week_k{k}days", "Mean", "StdDev"]]
    df_thresh.loc[:, "threshold_method"] = "previousNweeks"
    df_thresh.to_csv(output_path, index=False)
    return df_thresh


def historical_threshold_params(*, df, output_path, Ny=None, ex_y=None):
    """
    Compute the mean and std of 'case' from all previous years with the same region_id, month, and weekday.

    Parameters
    ----------
    df : pd.DataFrame
        Must have columns: region_id, date, case
    output_path : str or Path
        The path to the file containing the threshold values.
    Ny : int, optional
        The number of years of data we want to use for computing thresholds. The default is None.
    ex_y : list of int, optional
        The (excluded) years for which we do not want to use data for computing thresholds.
        The default is None.

    Returns
    -------
    df_thresh : pd.DataFrame
        Original df with added columns: Mean, StdDev
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)  # Ensure the parent directory exists

    if ex_y is None:
        ex_y = []
    if Ny is None:
        Ny = np.inf
    df = deepcopy(df)
    df["year"] = df["date"].dt.year
    df["month"] = df["date"].dt.month
    df["weekday"] = df["date"].dt.strftime("%a")

    all_years = sorted(df["year"].unique())
    result_rows = []
    for year in all_years:
        current_year_data = df[df["year"] == year]
        c1 = df["year"] < year
        c2 = df["year"] >= year - Ny
        c3 = ~df["year"].isin(ex_y)

        if (Ny is not np.inf) and (ex_y is not None):
            condition = c1 & c2 & c3
        elif (Ny is np.inf) and (ex_y is not None):
            condition = c1 & c3
        elif (Ny is not np.inf) and (ex_y is None):
            condition = c1 & c2
        else:
            condition = c1

        past_data = df[condition].reset_index(drop=True)

        if past_data.empty:  # No prior data — just assign NaN
            current_year_data = current_year_data.copy()
            current_year_data["Mean"] = np.nan
            current_year_data["StdDev"] = np.nan
        else:  # Group past data by (region, month, weekday) and compute stats
            stats = past_data.groupby(["region_id", "month", "weekday"])["case"].agg(Mean="mean", StdDev="std").reset_index()
            # Merge current year rows with historical stats
            current_year_data = pd.merge(current_year_data, stats, how="left", on=["region_id", "month", "weekday"])

        result_rows.append(current_year_data)

    # Combine all years
    df_thresh = pd.concat(result_rows, ignore_index=True)
    df_thresh = df_thresh.sort_values(["region_id", "date"]).reset_index(drop=True)
    df_thresh.loc[:, "threshold_method"] = "historical"
    df_thresh.to_csv(output_path, index=False)
    return df_thresh


# %% Combine thresholds
def combined_thresholds(*, list_df, output_path, cols, sort_by_cols, sort_order):
    """
    Combine the thresholds coming from different methods.

    Parameters
    ----------
    list_df : list of pd.DataFrame
        List of threshold dataframes that we want to concatenate. Must have columns listed in cols
    output_path : str or Path
        The path to the file containing the threshold values from multiple methods.
    cols : list of str
        The list of columns that should be there in all the thrshold dataframes.
    sort_by_cols : list of str
        The list of columns according to which we want to sort the concatenated output dataframe.
    sort_order: list of bool
        The order in which we want to sort the output dataframe: which column in increasing order and which one otherwise.

    Returns
    -------
    df_thresh : pd.DataFrame
        Original df with added columns: Mean, StdDev
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)  # Ensure the parent directory exists
    list_df = [val[cols].reset_index(drop=True) for val in list_df]
    df_thresh = pd.concat(list_df, ignore_index=True)
    df_thresh = df_thresh.sort_values(by=sort_by_cols, ascending=sort_order).reset_index(drop=True)
    df_thresh.to_csv(output_path, index=False)
    return df_thresh


def generate_thresholds(region_type: str, threshold_combinations):
    logger.info("Generating thresholds")

    df_align = align_dates_all_regions(input_file_path=f"./datasets/cases_{region_type}.csv")

    os.makedirs(Path("datasets/thresholds"), exist_ok=True)

    df_prevN_thresholds = prev_Nweeks_threshold_params(
        df=df_align, output_path=Path(f"datasets/thresholds/{region_type}_previousNweeks.csv")
    )
    df_hist_thresholds = historical_threshold_params(
        df=df_align, output_path=Path(f"datasets/thresholds/{region_type}_historical.csv"), Ny=5, ex_y=[2020, 2021]
    )

    df_thresholds = combined_thresholds(
        list_df=[df_prevN_thresholds, df_hist_thresholds],
        output_path=Path(f"datasets/thresholds/{region_type}_all_thresholds.csv"),
        cols=["region_id", "date", "Mean", "StdDev", "threshold_method"],
        sort_by_cols=["region_id", "date", "threshold_method"],
        sort_order=[True, True, True],
    )

    df_thresholds["Zero"] = 0

    df_thresholds["T0.00"] = threshold_combinations[0][1] * df_thresholds["Mean"] + threshold_combinations[0][1] * df_thresholds["StdDev"]
    df_thresholds["T1.00"] = threshold_combinations[1][1] * df_thresholds["Mean"] + threshold_combinations[1][1] * df_thresholds["StdDev"]
    df_thresholds["T2.00"] = threshold_combinations[2][1] * df_thresholds["Mean"] + threshold_combinations[2][1] * df_thresholds["StdDev"]

    df_thresholds["Inf"] = np.inf
    df_thresholds["thresholdMethod"] = "combined"
    # df_thresholds = df_thresholds.rename(columns={"region_id": "district"})

    df_thresholds["ISOWeek"] = df_thresholds["date"].dt.isocalendar().week

    return df_thresholds

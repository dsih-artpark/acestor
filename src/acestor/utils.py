import logging
import pandas as pd
import geopandas as gpd
from shapely.geometry import Point
import re
import numpy as np
from copy import deepcopy
from datetime import datetime, date, timedelta
import os


def ret_fmt(df, datecol):
    """Infer date format."""
    sample_date = df[datecol].dropna().iloc[0]
    if sample_date[2] == "-" and sample_date[5] == "-":
        fmt = "%d-%m-%Y"
    elif sample_date[4] == "-" and sample_date[7] == "-":
        fmt = "%Y-%m-%d"
    return fmt


def lat_long_to_ward():
    # Load the new CSV data
    cases_df = pd.read_csv("./datasets/std-bbmp-line-list.csv")
    cases_df.dropna(subset=["location.geometry.longitude", "location.geometry.latitude"], how="any", inplace=True)

    # Ensure the date column is in datetime format; assuming the date column is named 'report_date'
    cases_df["recordDate"] = pd.to_datetime(cases_df["metadata.primaryDate"])

    # Convert latitude and longitude to numeric, handling non-numeric values gracefully
    cases_df["longitude"] = pd.to_numeric(cases_df["location.geometry.longitude"], errors="coerce")
    cases_df["latitude"] = pd.to_numeric(cases_df["location.geometry.latitude"], errors="coerce")

    # Filter out rows where latitude, longitude, or date are NaN
    cases_df = cases_df.dropna(subset=["longitude", "latitude", "recordDate"])

    # Keep the columns that are useful in the current context
    cases_df = cases_df[["longitude", "latitude", "recordDate"]]

    # Convert the new CSV DataFrame to a GeoDataFrame with geometry
    geometry = [Point(xy) for xy in zip(cases_df["longitude"], cases_df["latitude"])]
    cases_gdf = gpd.GeoDataFrame(cases_df, geometry=geometry, crs="EPSG:4326")

    # Load the GeoJSON file for wards
    wards_gdf = gpd.read_file("./geojsons/bbmp_ward.geojson")

    # Extract only the name within the parentheses from the 'name' column
    wards_gdf["wardName"] = wards_gdf["name"].apply(lambda x: re.search(r"\((.*?)\)", x).group(1) if re.search(r"\((.*?)\)", x) else x)

    # Align cases_gdf to the CRS of wards_gdf if they are different
    if cases_gdf.crs != wards_gdf.crs:
        cases_gdf = cases_gdf.to_crs(wards_gdf.crs)

    # Perform the spatial join
    wardLineList = gpd.sjoin(cases_gdf, wards_gdf, how="inner", op="within")
    # wardLineList['wardName'] = wardLineList['wardName'].apply(lambda x: x.title())
    return wardLineList


def retWardCases(wardLineList):
    # Group cases by ward and recordDate
    wardCaseCounts = wardLineList.groupby(["wardName", "recordDate"]).size().reset_index(name="case_count")
    # wardCaseCountsRelevant = wardCaseCounts[(wardCaseCounts['recordDate'] >= (pd.to_datetime(datetime.today()-timedelta(days=30))).tz_localize('UTC')) &
    # (wardCaseCounts['recordDate'] <= pd.to_datetime(datetime.today()).tz_localize('UTC'))]

    # Sort primarily by ward name, and then by recordDate for chronological order within each ward
    wardCaseCounts = wardCaseCounts.sort_values(by=["wardName", "recordDate"]).reset_index(drop=True)
    # wardCaseCountsRelevant = wardCaseCountsRelevant.sort_values(by=['wardName', 'recordDate']).reset_index(drop=True)
    # wardsDates = wardCaseCounts.groupby(['recordDate']).size().reset_index(name='NoOfWards')
    # wardsDatesRelevant = wardCaseCountsRelevant.groupby(['recordDate']).size().reset_index(name='NoOfWards')

    # # Assuming ward_month_case_counts is already loaded and grouped by ['name', 'recordDate']
    # totalWardCases = wardCaseCounts.groupby('wardName').agg(total_cases=('case_count', 'sum')).reset_index()
    return wardCaseCounts


def weekCasesWards(wardCaseCounts, weeks=3):
    dfWeeklyAggregateDict = {}
    for i in range(weeks):
        dfIthWeekCases = wardCaseCounts[
            (wardCaseCounts["recordDate"] > (pd.to_datetime(datetime.today() - (i + 1) * timedelta(days=7))).tz_localize("UTC"))
            & (wardCaseCounts["recordDate"] <= (pd.to_datetime(datetime.today() - i * timedelta(days=7))).tz_localize("UTC"))
        ]
        dfIthWeekCasesAggregate = dfIthWeekCases.groupby(["wardName"])["case_count"].sum().reset_index(name="Cases")
        dfWeeklyAggregateDict[pd.to_datetime(datetime.today() - i * timedelta(days=7)).strftime("%Y-%m-%d")] = dfIthWeekCasesAggregate

    dfWardWeeklyCases = pd.DataFrame()
    listWards = wardCaseCounts["wardName"].unique()
    dfWardWeeklyCases.loc[:, "wardName"] = listWards
    for dfThis in dfWeeklyAggregateDict:
        dfWardWeeklyCases = pd.merge(dfWardWeeklyCases, dfWeeklyAggregateDict[dfThis], on="wardName", how="outer")
        dfWardWeeklyCases.rename(columns={"Cases": dfThis}, inplace=True)
    dfWardWeeklyCases.to_csv("./summary/BBMPWardWeeklyCases.csv")
    return dfWardWeeklyCases


def weekCasesWards_AllWeeks(config, wardCaseCounts):
    # Aggregate linelist data by spatial resolution, temporal resolution, and year
    dfWardWeeklyCases = (
        wardCaseCounts.groupby(["wardName", pd.Grouper(key="recordDate", freq=config["tempo_res"])])
        .agg(case=("case_count", "sum"))
        .reset_index()
    )
    wardCaseCounts = wardCaseCounts.sort_values(by=["wardName", "recordDate"]).reset_index(drop=True)

    dfWardWeeklyCases.to_csv("BBMPWardWeeklyCases_All.csv")
    return dfWardWeeklyCases


def process_linelist_data_ihip(config):
    # Load raw linelist data
    linelist_df = pd.read_csv(config["linelist_data_ihip"], low_memory=False)

    # Drop NA
    logging.info(f"Shape of linelist data before dropping NA: {linelist_df.shape}")
    linelist_df.dropna(subset=["metadata.primaryDate"], inplace=True)
    logging.info(f"Shape of linelist data after dropping NA: {linelist_df.shape}")

    linelist_df["recordDate"] = pd.to_datetime(linelist_df["metadata.primaryDate"], errors="coerce")
    linelist_df["recordDate"] = linelist_df["recordDate"].dt.date.astype("datetime64[ns]")
    linelist_df.rename(columns={"location.admin3.ID": "subdistrict"}, inplace=True)
    linelist_df.rename(columns={"location.admin2.ID": "district"}, inplace=True)

    # Aggregate linelist data by spatial resolution, temporal resolution, and year
    case_data = (
        linelist_df.groupby(["region_id", pd.Grouper(key="recordDate", freq=config["tempo_res"])])
        .agg(case=("metadata.diseaseName", "count"))
        .reset_index()
    )

    # Extract year, month, ISO week, and ISO year
    case_data["recordYear"] = case_data["recordDate"].dt.year.astype(int)
    case_data["recordMonth"] = case_data["recordDate"].dt.month.astype(int)
    case_data["ISOWeek"] = case_data["recordDate"].dt.isocalendar().week.astype(int)
    # case_data['ISOYear'] = case_data['recordDate'].dt.isocalendar().year.astype(int)

    # Find the last week of the year
    # last_week_year_end = datetime(case_data['recordYear'].max(), 12, 31).isocalendar()[1]

    # Check if the last week of the year belongs entirely to the current year
    # if last_week_year_end == 1:
    #     case_data['ISOWeek'] = case_data.apply(lambda x: x['ISOWeek'] - 1 if x['ISOWeek'] == 53 else x['ISOWeek'], axis=1)

    case_data["ISOWeek"] = case_data["ISOWeek"].astype(int)
    case_data["recordMonth"] = case_data["recordMonth"].astype(int)

    # Find the last available date for case data
    last_available_date = case_data["recordDate"].max()
    logging.warning(f"Last available date for case data: {last_available_date}")

    # Check if ISO conventions are followed
    if not case_data["recordDate"].dt.weekday.eq(0).all():
        logging.warning("ISO conventions may not be followed in case_data.")
    case_data.sort_values(by=["region_id", "recordDate"], ascending=[True, True], inplace=True)

    return case_data


def process_linelist_data(config):
    # Load raw linelist data
    linelist_df = pd.read_csv(config["linelist_data"], low_memory=False)

    # Drop NA
    logging.info(f"Shape of linelist data before dropping NA: {linelist_df.shape}")
    linelist_df.dropna(subset=["metadata.primaryDate"], inplace=True)
    logging.info(f"Shape of linelist data after dropping NA: {linelist_df.shape}")

    linelist_df["recordDate"] = pd.to_datetime(linelist_df["metadata.primaryDate"], errors="coerce")
    linelist_df["recordDate"] = linelist_df["recordDate"].dt.date.astype("datetime64[ns]")
    linelist_df.rename(columns={"location.admin3.ID": "subdistrict"}, inplace=True)
    linelist_df.rename(columns={"location.admin2.ID": "district"}, inplace=True)

    # Aggregate linelist data by spatial resolution, temporal resolution, and year
    case_data = (
        linelist_df.groupby(["region_id", pd.Grouper(key="recordDate", freq=config["tempo_res"])])
        .agg(case=("metadata.diseaseName", "count"))
        .reset_index()
    )

    # Extract year, month, ISO week, and ISO year
    case_data["recordYear"] = case_data["recordDate"].dt.year.astype(int)
    case_data["recordMonth"] = case_data["recordDate"].dt.month.astype(int)
    case_data["ISOWeek"] = case_data["recordDate"].dt.isocalendar().week.astype(int)
    # case_data['ISOYear'] = case_data['recordDate'].dt.isocalendar().year.astype(int)

    # Find the last week of the year
    # last_week_year_end = datetime(case_data['recordYear'].max(), 12, 31).isocalendar()[1]

    # Check if the last week of the year belongs entirely to the current year
    # if last_week_year_end == 1:
    #     case_data['ISOWeek'] = case_data.apply(lambda x: x['ISOWeek'] - 1 if x['ISOWeek'] == 53 else x['ISOWeek'], axis=1)

    case_data["ISOWeek"] = case_data["ISOWeek"].astype(int)
    case_data["recordMonth"] = case_data["recordMonth"].astype(int)

    # Find the last available date for case data
    last_available_date = case_data["recordDate"].max()
    logging.warning(f"Last available date for case data: {last_available_date}")

    # Check if ISO conventions are followed
    if not case_data["recordDate"].dt.weekday.eq(0).all():
        logging.warning("ISO conventions may not be followed in case_data.")
    case_data.sort_values(by=["region_id", "recordDate"], ascending=[True, True], inplace=True)

    return case_data


def process_weather_data(config, root_dir, granularity, region_name):
    # Load weather data
    # weather_data = pd.read_csv(config['weather_data'])
    weather_data = pd.read_csv(
        os.path.join(
            root_dir / "datasets",
            next(
                file
                for file in os.listdir("datasets")
                if (
                    ("processed_aggregated_era5" in file)
                    and (file.endswith(".csv"))
                    and (granularity.capitalize() in file)
                    and (region_name.capitalize() in file)
                )
            ),
        )
    )
    date_fmt = ret_fmt(weather_data, datecol="metadata.primaryDate")
    weather_data["recordDate"] = pd.to_datetime(weather_data["metadata.primaryDate"], format=date_fmt)
    weather_data["recordYear"] = weather_data["recordDate"].dt.year
    weather_data["ISOWeek"] = weather_data["recordDate"].dt.isocalendar().week
    weather_data.sort_values(by=["region_id", "recordDate"], ascending=True, inplace=True)

    # Check if ISO conventions are followed
    if not weather_data["recordDate"].dt.weekday.eq(0).all():
        logging.warning("ISO conventions may not be followed in weather_data.")

    # Check if latest data covers the last four weeks
    current_iso_year, current_iso_week = datetime.now().isocalendar()[:2]
    latest_data_iso_year = weather_data["recordYear"].max()
    latest_data_iso_week = weather_data[weather_data["recordYear"] == latest_data_iso_year]["ISOWeek"].max()

    # Find the last available date for weather data
    last_available_date = weather_data["recordDate"].max()
    logging.warning(f"Last available date for weather data: {last_available_date}")
    total_available_weeks = len(weather_data["recordDate"].unique())
    logging.info(f"Total available weeks for weather data: {total_available_weeks}")

    if latest_data_iso_year != current_iso_year or latest_data_iso_week < current_iso_week - 4:
        logging.warning(
            f"Latest weather data may not cover the last four weeks from the current ISO week (Year: {current_iso_year}, Week: {current_iso_week}). Latest available ISO week is {latest_data_iso_week} for the {latest_data_iso_year}"
        )

    return weather_data


def process_case_data(config, root_dir, granularity):
    # Load weather data
    # weather_data = pd.read_csv(config['weather_data'])
    region_type = granularity
    case_data = pd.read_csv(
        os.path.join(root_dir / "datasets", next(file for file in os.listdir("datasets") if (file == f"cases_{region_type}_sampled.csv")))
    )
    date_fmt = ret_fmt(case_data, datecol="metadata.primaryDate")
    case_data["recordDate"] = pd.to_datetime(case_data["metadata.primaryDate"], format=date_fmt)
    case_data["recordYear"] = case_data["recordDate"].dt.year
    case_data["ISOWeek"] = case_data["recordDate"].dt.isocalendar().week
    case_data.sort_values(by=["region_id", "recordDate"], ascending=True, inplace=True)

    # Check if ISO conventions are followed
    if not case_data["recordDate"].dt.weekday.eq(0).all():
        logging.warning("ISO conventions may not be followed in case_data.")

    # Check if latest data covers the last four weeks
    current_iso_year, current_iso_week = datetime.now().isocalendar()[:2]
    latest_data_iso_year = case_data["recordYear"].max()
    latest_data_iso_week = case_data[case_data["recordYear"] == latest_data_iso_year]["ISOWeek"].max()

    # Find the last available date for weather data
    last_available_date = case_data["recordDate"].max()
    logging.warning(f"Last available date for case data: {last_available_date}")
    total_available_weeks = len(case_data["recordDate"].unique())
    logging.info(f"Total available weeks for case data: {total_available_weeks}")

    if latest_data_iso_year != current_iso_year or latest_data_iso_week < current_iso_week - 4:
        logging.warning(
            f"Latest case data may not cover the last four weeks from the current ISO week (Year: {current_iso_year}, Week: {current_iso_week}). Latest available ISO week is {latest_data_iso_week} for the {latest_data_iso_year}"
        )

    return case_data


# Merge case data with weather data
def merge(config, case_data, weather_data):
    merged_df = pd.merge(
        case_data,
        weather_data,
        on=["region_id", "metadata.primaryDate", "recordDate", "recordYear", "ISOWeek"],
        how="outer",
    )
    merged_df.to_csv("datasets/merged_in_utils_df_debug.csv", index=False)
    merged_df.sort_values(by=["region_id", "recordDate"], ascending=True, inplace=True)
    merged_df.reset_index(drop=True, inplace=True)
    current_date = datetime.now().strftime("%Y-%m-%d")
    logging.info(f"Current Date: {current_date}")
    return merged_df


# Threshold functions  (Tarun)


def GenDateList(dfTemp, to_date=None):
    """
    Fetch the dates from the dataframe

    Parameters
    ----------
    dfTemp : pandas dataframe
        The weekly cases dataframe. It may have some weeks missing.

    Returns
    -------
    ListDates : list of datetime values
        The full list of dates including the missing ones in dfTemp.

    """
    if not isinstance(to_date, date):
        to_date = datetime.now().date()
    dateList = list(dfTemp["recordDate"].unique())
    if len(dateList) > 0:
        ListDates = [val.date() for val in pd.date_range(to_date, dateList[0], freq="-7D")][::-1]
        return ListDates
    else:
        return []


def GenMissingDateRows(dfTemp, to_date=None):
    """
    Generates the rows corresponding to the missing dates in dfTemp

    Parameters
    ----------
    dfTemp : pandas dataframe
        The dataframe which may have missing dates.

    Returns
    -------
    dfTemp : pandas dataframe
        The dataframe which has rows corresponding to missing dates.

    """
    if not isinstance(to_date, date):
        to_date = datetime.now().date()
    dates = GenDateList(dfTemp, to_date)
    dfTemp = dfTemp.set_index("recordDate").reindex(dates).reset_index()
    return dfTemp


def retNAfilledDF(config, df, to_date=None):
    """
    Returns the datframe containing all the dates with missing data across all regions

    Parameters
    ----------
    df : pandas dataframe
        The dataframe may not have all the dates. We want to fill all the missing ones.
    region : TYPE

    Returns
    -------
    dfFilled : TYPE
        DESCRIPTION.

    """
    if not isinstance(to_date, date):
        to_date = datetime.now().date()
    region = "region_id"
    # df = df[~df["recordDate"].isna()]
    df.to_csv("datasets/debug/retnafilleddff.csv", index=False)
    listdf = [GenMissingDateRows(df[df[region] == val], to_date) for val in list(df[region].unique())]
    dfFilled = pd.concat(listdf, ignore_index=True)
    dfFilled.loc[:, region] = dfFilled[region].ffill()
    dfFilled["recordDate"] = pd.to_datetime(dfFilled["recordDate"])
    dfFilled.loc[:, "recordYear"] = dfFilled["recordDate"].dt.year
    dfFilled.loc[:, "recordMonth"] = dfFilled["recordDate"].dt.month
    return dfFilled


def retHistoricalStats(config, region_aggregated, to_date=None):
    # Compute historical mean and standard deviation
    region_aggregated = retNAfilledDF(config, region_aggregated, to_date)
    historical_stats = region_aggregated.groupby(["region_id", "recordMonth"]).agg({"case": ["mean", "std"]})
    historical_stats.columns = ["Mean", "StdDev"]
    # merge region_aggregated with historical_stats
    historical_stats = pd.merge(region_aggregated, historical_stats, on=["recordMonth", "region_id"], how="left")
    historical_stats.loc[:, "ISOWeek"] = historical_stats["recordDate"].apply(lambda x: datetime.isocalendar(x).week)
    historical_stats.sort_values(by=["region_id", "recordDate", "recordMonth", "ISOWeek"], ascending=[True, True, True, True], inplace=True)
    historical_stats["thresholdMethod"] = "historical"
    historical_stats.reset_index(drop=True)
    return historical_stats


def retPrevNWeeksStats(config, region_aggregated, NoPreviousWeeks=4, to_date=None):
    # For each region: At week N, compute mean and std of previous 4 weeks (N-1 to N-4)
    region_aggregated = retNAfilledDF(config, region_aggregated, to_date)
    reg_agg = deepcopy(region_aggregated).reset_index(drop=True)
    rolling_mean = reg_agg.groupby(["region_id"])["case"].rolling(window=NoPreviousWeeks, min_periods=1, closed="left").mean().reset_index()
    rolling_std = rolling_mean.groupby(["region_id"])["case"].rolling(window=NoPreviousWeeks, min_periods=1).std().reset_index()
    mean_rolling_mean = rolling_mean.groupby(["region_id"])["case"].rolling(window=3, min_periods=1).mean().reset_index()

    reg_agg.loc[:, "Mean_4week"] = rolling_mean["case"]
    reg_agg.loc[:, "Mean_3_Mean_4week"] = mean_rolling_mean["case"]
    reg_agg.loc[:, "StdDev_4week"] = rolling_std["case"]

    # Take only unique combinations of spatial_res, ISO_Week, and previously computed values
    previous_stats = reg_agg.drop_duplicates(subset=["region_id", "ISOWeek", "Mean_4week", "StdDev_4week", "Mean_3_Mean_4week"])
    previous_stats = previous_stats.rename(columns={"Mean_3_Mean_4week": "Mean", "StdDev_4week": "StdDev"})

    # merge region_aggregated with previous_stats
    previous_stats = pd.merge(
        reg_agg[["region_id", "recordDate", "recordMonth", "ISOWeek"]],
        previous_stats,
        on=["region_id", "recordDate", "recordMonth", "ISOWeek"],
        how="left",
    )
    previous_stats.loc[:, "ISOWeek"] = previous_stats["recordDate"].apply(lambda x: datetime.isocalendar(x).week)
    previous_stats.sort_values(by=["region_id", "recordDate", "recordMonth", "ISOWeek"], ascending=[True, True, True, True], inplace=True)
    previous_stats["thresholdMethod"] = "previousNweeks"
    previous_stats.reset_index(drop=True)
    return previous_stats


def mu_sigma(config, case_data, to_date=None):
    """
    Compute the mean (mu) and standard deviation (sigma) based on method (historical or  previous n weeks)

    Parameters
    ----------
    config : TYPE
        DESCRIPTION.
    case_data : TYPE
        DESCRIPTION.

    Returns
    -------
    dataframe with mean and standard deviation

    """
    # Add missing dates
    case_data0 = retNAfilledDF(config, case_data, to_date)
    logging.info(f"Length of case data after filling missing dates: {len(case_data0)}")

    # Sort
    region_aggregated = case_data0.sort_values(by=["region_id", "recordDate"], ascending=[True, True]).reset_index(drop=True)
    historical_stats = retHistoricalStats(config, region_aggregated, to_date)
    region_aggregated.to_csv("datasets/debug/region_aggregated.csv", index=False)
    previous_stats = retPrevNWeeksStats(config, region_aggregated, NoPreviousWeeks=4, to_date=to_date)

    # # Drop NaN values introduced by rolling window
    # reg_agg.dropna(subset=['Mean_4week', 'StdDev_4week', 'Mean_3_Mean_4week'], inplace=True)

    # if reg_agg.notna().all().all():
    #     logging.info('Threshold dataframe has no NaN rows and columns')
    # else:
    #     logging.warning('Threshold dataframe contains NaN rows or columns')
    dfMuSigma = pd.concat([historical_stats, previous_stats]).reset_index(drop=True)
    dfMuSigma.rename(columns={"Mean_3_Mean_4week": "Mean", "StdDev_4week": "StdDev"}, inplace=True)
    return dfMuSigma


def compute_thresholds(config, case_data, to_date=None):
    case_data.to_csv("datasets/debug/compute_thresholds.csv", index=False)
    df = mu_sigma(config, case_data, to_date)
    listAlpha = config["listAlpha"]
    thresholds_df = deepcopy(df)

    # Add column of zero and inf
    thresholds_df.loc[:, "Zero"] = 0.0
    thresholds_df.loc[:, "Inf"] = np.inf

    # Calculate thresholds
    listAlpha = [0.0] + listAlpha
    for i in range(len(listAlpha)):
        thresholds_df.loc[:, f"T{listAlpha[i]:.2f}"] = thresholds_df[["Mean", "StdDev"]].apply(
            lambda x: x.iloc[0] + listAlpha[i] * x.iloc[1], axis=1
        )

    # Add epsilon noise to separate T1.00, T2.00, etc. and 2*epsilon to T2.00
    epsilon = 1e-6
    for i in range(1, len(listAlpha)):
        thresholds_df.loc[:, f"T{listAlpha[i]:.2f}"] += i * epsilon

    return thresholds_df


def retThresholdPairs(df_thresholds):
    """
    Use the dataframe of thresholds to generate the list of column pairs (lower limit, upper limit).
    The cases, actual or predicted, will then be compared with these pairs to assign the zone label.

    Parameters
    ----------
    df_thresholds : pandas datafram containing the information of thresholds
        DESCRIPTION.

    Returns
    -------
    ThresholdPairs : tuple of strings (column headers of df_thresholds)

    """
    ThresholdColumns = [None] + [val for val in df_thresholds.columns if ((("T" in val) and ("." in val)) or ("Thresh" in val))] + [None]

    ThresholdPairs = []  # [v1,v2] for v1,v2 in zip(ThresholdColumns[:-1], ThresholdColumns[1:])]
    for v1, v2 in zip(ThresholdColumns[:-1], ThresholdColumns[1:]):
        if v1 is None:
            ThresholdPairs.append(("Zero", v2))
        elif v2 is None:
            ThresholdPairs.append((v1, "Inf"))
        else:
            ThresholdPairs.append((v1, v2))
    return ThresholdPairs


def AssignZone(df, ThresholdPairs):
    """
    Based on the cases predicted by a model and the thresholds, assign zone label for each prediction

    Parameters
    ----------
    df : pandas dataframe
        Contains the model predictions
    ThresholdPairs : list of lists
        The list of column headers that will be used as threshold pairs
    Returns
    -------
    dfFilter : pandas dataframe
        The dataframe contining the zone label (string and numerical) values

    """
    dfZone = deepcopy(df)
    for i in range(len(ThresholdPairs)):
        th_0, th_1 = ThresholdPairs[i]
        zone = i + 1
        dfZone.loc[((dfZone["prediction"] >= dfZone[th_0]) & (dfZone["prediction"] < dfZone[th_1])), "predictionZone"] = zone
    return dfZone


# def mergePredictionsThresholds(config, case_data, model_pred, to_date=None, noOfDays=28, thresholds_df=None):
#     # thresholds_df = compute_thresholds(config, case_data, to_date)
#     # thresholds_df.to_csv("datasets/debug/thresholds.csv", index=False)
#     # thresholds_df = thresholds_df[thresholds_df["recordDate"] == to_date]
#     # Add the number of days so that thresholds can be compared with predictions
#     # thresholds_df.loc[:, 'recordDate'] = thresholds_df['recordDate'].apply(lambda x: x + timedelta(days=noOfDays))
#     # thresholds_df["recordDate"] = pd.to_datetime(thresholds_df["recordDate"])
#     df_columns = ["region_id", "recordDate", "prediction", "model"]
#     thresholds_columns = ["region_id", "ISOWeek", "thresholdMethod", "Mean", "StdDev", "Zero", "Inf"]
#     thresholds_columns += [f"T{val:.2f}" for val in [0.0] + config["listAlpha"]]
#     df_predictions = pd.merge(model_pred[df_columns], thresholds_df[thresholds_columns], on=["region_id"], how="left")

#     df_predictions["startDatePredictedWeek"] = df_predictions["recordDate"]
#     current_date = datetime.now().strftime("%Y-%m-%d")
#     df_predictions["dateOfComputingPrediction"] = current_date
#     df_predictions["regionID"] = df_predictions["region_id"]

#     # Sort data by Subdistrict and Record_Date
#     df_predictions.sort_values(by=["region_id", "recordDate"], inplace=True)
#     df_predictions.reset_index(drop=True, inplace=True)
#     df_predictions[
#         ["dateOfComputingPrediction", "startDatePredictedWeek", "regionID", "prediction", "Mean", "StdDev", "thresholdMethod", "model"]
#         + thresholds_columns
#     ]

#     df_predictions.to_csv("datasets/debug/thresholds_return_df.csv", index=False)

#     return df_predictions


def assignCommonThreshold(df):
    model_df_list = []
    for model_name, model_data in df.groupby("model"):
        region_df_list = []
        for region_name, region_data in model_data.groupby("regionID"):
            threshold_df_list = []
            for threshold_name, threshold_data in region_data.groupby("thresholdMethod"):
                threshold_data.sort_values(by="startDatePredictedWeek", inplace=True)
                last_threshold_Mean = list(threshold_data["Mean"])[-1]
                last_threshold_StdDev = list(threshold_data["StdDev"])[-1]
                threshold_data.loc[:, "Mean"] = last_threshold_Mean
                threshold_data.loc[:, "StdDev"] = last_threshold_StdDev
                threshold_df_list.append(threshold_data)
            if len(threshold_df_list) > 0:
                region_df_list.append(pd.concat(threshold_df_list, ignore_index=True))
        if len(region_df_list) > 0:
            model_df_list.append(pd.concat(region_df_list, ignore_index=True))
    dfProcessedThresholds = pd.concat(model_df_list, ignore_index=True)
    return dfProcessedThresholds


def classifyIntoZones(config, df_predictions):
    # Add zone columns
    ThresholdPairs = retThresholdPairs(df_predictions)
    dfZones = AssignZone(df_predictions, ThresholdPairs)

    # Add Prediction dates
    current_date = datetime.now().strftime("%Y-%m-%d")
    dfZones["dateOfComputingPrediction"] = current_date
    dfZones["regionID"] = dfZones["region_id"]

    # Check if T0.00 < T1.00 < T2.00 for non-null rows
    non_null_rows = df_predictions[["T0.00", "T1.00", "T2.00"]].notnull().all(axis=1)
    if (
        not ((df_predictions["T0.00"] < df_predictions["T1.00"]) & (df_predictions["T1.00"] < df_predictions["T2.00"]))
        .loc[non_null_rows]
        .all()
    ):
        logging.warning("Thresholds are not in ascending order for all non-NaN rows.")

    return dfZones


def save_prediction_output(config, output, output_directory="prediction_output/"):
    """
    Save the prediction output to a specified directory with a filename based on the spatial resolution from the config.

    Parameters:
    - output (pd.DataFrame): The prediction output to be saved.
    - output_directory (str): The directory where the output file should be saved. Default: prediction_output/
    - config (dict): Configuration dictionary containing 'spatial_res' key for naming the output file.

    Returns:
    - str: The path to the saved output file.
    """
    if not os.path.exists(output_directory):
        os.makedirs(output_directory)
    filename = f"prediction_{output['model'].iloc[0]}_{config['spatial_res']}.csv"
    output_file_path = os.path.join(output_directory, filename)
    output.to_csv(output_file_path, index=False)
    logging.info(f"Prediction output saved to {output_file_path}")
    return output_file_path

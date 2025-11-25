import pandas as pd
import utils
import models.tse as tse
import models.nbr as nbr
import yaml
import logging
import os
import pickle

from copy import deepcopy
from datetime import datetime
from pathlib import Path


# %%
def return_months_names(listdates):
    """Return month names using a list of dates."""
    listmonths = [f"{val.strftime('%b')}{val.strftime('%y')}" for val in listdates]
    uniquemonthlist = []
    for val in listmonths:
        if val not in uniquemonthlist:
            uniquemonthlist.append(val)
    monthstring = "_".join(uniquemonthlist)
    return monthstring


def get_month_year_range(listdates):
    """From a list of dates, generate a string which indicates the month range."""
    # Ensure all dates are pd.Timestamp and drop duplicates
    unique_months = sorted({(d.year, d.month) for d in listdates})

    # Format helper
    def format_month_year(y, m):
        return pd.Timestamp(year=y, month=m, day=1).strftime("%b %Y")

    def format_month(m):
        return pd.Timestamp(year=2000, month=m, day=1).strftime("%b")

    if not unique_months:
        logging.info("WARNING: The list of dates is empty!")
        return ""

    # Extract first and last entries
    (start_year, start_month), (end_year, end_month) = unique_months[0], unique_months[-1]

    if len(unique_months) == 1:
        return format_month_year(start_year, start_month)

    elif start_year == end_year:
        # Same year, different months
        return f"{format_month(start_month)} - {format_month(end_month)} {start_year}"

    else:
        # Different years
        return f"{format_month(start_month)} {start_year} - {format_month(end_month)} {end_year}"


# %%
def get_mondays(start_date, end_date):
    """Generate a date range between start_date and end_date."""
    dates = pd.date_range(start=start_date, end=end_date, freq="W-MON")
    return dates.tolist()


def get_days(start_date, end_date, sampling_day):
    """Generate a date range between start_date and end_date."""
    dates = pd.date_range(start=start_date, end=end_date, freq=sampling_day)
    return dates.tolist()


def run_district_predictions(root_dir: Path, pred_upto, cutoff_case, sampling_day):
    with open(root_dir / "config/config_district.yaml", "r") as f:
        config = yaml.safe_load(f)

    case_data = utils.process_case_data(config, root_dir)  # %% Load weather data
    weather_data = utils.process_weather_data(config, root_dir)

    case_data.to_csv("datasets/debug/processed_case_data.csv", index=False)
    weather_data.to_csv("datasets/debug/processed_weather_data.csv", index=False)

    # %% Merge cases and weather data
    merged_df = utils.merge(config, case_data, weather_data)
    merged_df.to_csv("datasets/debug/merged_df.csv", index=False)
    listValidDates = get_days(min(merged_df["recordDate"]), pred_upto, sampling_day=sampling_day)
    merged_df = merged_df.sort_values([config["spatial_res"], "recordDate"]).reset_index(drop=True)
    merged_df = merged_df[merged_df["recordDate"].isin(listValidDates)].reset_index(drop=True)

    # %% Model Estimation and Prediction
    """ NEGATIVE BINOMIAL REGRESSION """
    pred_upto_date = pred_upto
    to_date = pred_upto_date - pd.Timedelta(days=28)
    logging.info(f"shape of merged_df: {merged_df.shape}")
    nbr_pred = nbr.negative_binomial_regression(config, merged_df, predict_upto_date=pred_upto_date)
    output_nbr = utils.mergePredictionsThresholds(config, case_data, nbr_pred, to_date, noOfDays=28)
    output_nbr[output_nbr["startDatePredictedWeek"] <= pred_upto]
    logging.info(output_nbr)

    """ TIME SERIES EXTRAPOLATION """
    pred_upto_date = cutoff_case.date() + pd.Timedelta(days=14)
    to_date = pred_upto_date - pd.Timedelta(days=14)

    if config["spatial_res"] != "district":
        logging.error("config['spatial_res'] should match 'district' column for time series extrapolation model to run.")
    else:
        tse_pred = tse.linear_extrapolation(config, case_data, predict_upto_date=pred_upto_date)
        output_tse = utils.mergePredictionsThresholds(config, case_data, tse_pred, to_date, noOfDays=14)
        output_tse[output_tse["startDatePredictedWeek"] <= pd.Timestamp(pred_upto_date)]
        logging.info(output_tse)

    if config["spatial_res"] == "district":
        predictions_df0 = pd.concat([output_nbr, output_tse]).reset_index(drop=True)
    else:
        predictions_df0 = output_nbr.reset_index(drop=True)

    cols_for_grouping = [val for val in predictions_df0.columns if val not in ["prediction", "model"]]

    predictions_df0.to_csv("datasets/debug/predictiondf0.csv", index=False)

    predictions_df1 = predictions_df0.groupby(cols_for_grouping)["prediction"].max().reset_index()
    if config["spatial_res"] == "district":
        predictions_df1.loc[:, "model"] = "ensembleModel"
    else:
        predictions_df1.loc[:, "model"] = "negativeBinomialRegression"

    predictions_df2 = utils.classifyIntoZones(config, predictions_df1)

    predictions_df = deepcopy(predictions_df2)
    predictions_df.loc[:, "dateOfComputingPrediction"] = datetime.now().strftime("%Y-%m-%d")
    predictions_df.sort_values(by=["regionID", "startDatePredictedWeek"], ascending=[True, True], inplace=True)
    predictions_df["predictionZone"] = predictions_df["predictionZone"].fillna(0)
    predictions_df["prediction"] = predictions_df["prediction"].fillna(0)

    listPredDates = list(predictions_df["startDatePredictedWeek"].unique())
    listPredDates = [val for val in listPredDates if val >= pd.Timestamp.today().normalize()]
    monthstring = get_month_year_range(listPredDates)
    endString = pd.Timestamp.today().date().strftime(format="%Y%m%d")

    # with open("dumps/monthstring.pkl", "wb") as f:
    #     pickle.dump(monthstring, f)

    predictions_df.to_csv(f"results/Predictions_{monthstring}_{config['spatial_res'].capitalize()}_{endString}.csv", index=False)

    cols_of_interest = [
        "dateOfComputingPrediction",
        "startDatePredictedWeek",
        "regionID",
        "prediction",
        "thresholdMethod",
        "predictionZone",
        "model",
    ]

    if config["spatial_res"] == "district":
        predictions_df = deepcopy(predictions_df2)
        predictions_df_state = (
            predictions_df.groupby(["startDatePredictedWeek", "model", "thresholdMethod"])["prediction"].sum().reset_index()
        )
        predictions_df_state.loc[:, "dateOfComputingPrediction"] = datetime.now().strftime("%Y-%m-%d")
        predictions_df_state.loc[:, "regionID"] = "state_29"
        predictions_df_state.loc[:, "predictionZone"] = 0
        predictions_df_state = predictions_df_state[cols_of_interest]
        predictions_df_state.drop_duplicates(inplace=True)
        predictions_df_state.to_csv(f"results/Predictions_{monthstring}_Karnataka_{endString}.csv", index=False)

    return predictions_df, predictions_df_state

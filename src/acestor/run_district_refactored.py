import pandas as pd
import utils
from models.tse import TSE
from models.nbr import NBR
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


def mergePredictionsThresholds(config, model_pred, thresholds_df=None):
    # thresholds_df = compute_thresholds(config, case_data, to_date)
    # thresholds_df.to_csv("datasets/debug/thresholds.csv", index=False)
    # thresholds_df = thresholds_df[thresholds_df["recordDate"] == to_date]
    # Add the number of days so that thresholds can be compared with predictions
    # thresholds_df.loc[:, 'recordDate'] = thresholds_df['recordDate'].apply(lambda x: x + timedelta(days=noOfDays))
    # thresholds_df["recordDate"] = pd.to_datetime(thresholds_df["recordDate"])
    df_columns = ["region_id", "recordDate", "prediction", "model"]
    thresholds_columns = ["region_id", "ISOWeek", "thresholdMethod", "Mean", "StdDev", "Zero", "Inf"]
    thresholds_columns += [f"T{val:.2f}" for val in [0.0] + config["listAlpha"]]
    print(thresholds_df.columns)
    df_predictions = pd.merge(model_pred[df_columns], thresholds_df[thresholds_columns], on=["region_id"], how="left")

    df_predictions["startDatePredictedWeek"] = df_predictions["recordDate"]
    current_date = datetime.now().strftime("%Y-%m-%d")
    df_predictions["dateOfComputingPrediction"] = current_date
    df_predictions["regionID"] = df_predictions["region_id"]

    # Sort data by Subdistrict and Record_Date
    df_predictions.sort_values(by=["region_id", "recordDate"], inplace=True)
    df_predictions.reset_index(drop=True, inplace=True)
    df_predictions[
        ["dateOfComputingPrediction", "startDatePredictedWeek", "regionID", "prediction", "Mean", "StdDev", "thresholdMethod", "model"]
        + thresholds_columns
    ]

    df_predictions.to_csv("datasets/debug/thresholds_return_df.csv", index=False)

    return df_predictions


def preprocess_case_and_weather_data(config, root_dir, pred_upto, sampling_day, granularity, region_name):
    case_data = utils.process_case_data(config, root_dir, granularity)  # %% Load weather data
    weather_data = utils.process_weather_data(config, root_dir, granularity, region_name)

    case_data.to_csv("datasets/debug/processed_case_data.csv", index=False)
    weather_data.to_csv("datasets/debug/processed_weather_data.csv", index=False)

    merged_df = utils.merge(config, case_data, weather_data)
    merged_df.to_csv("datasets/debug/merged_df.csv", index=False)
    listValidDates = get_days(min(merged_df["recordDate"]), pred_upto, sampling_day=sampling_day)
    merged_df = merged_df.sort_values(["region_id", "recordDate"]).reset_index(drop=True)
    merged_df = merged_df[merged_df["recordDate"].isin(listValidDates)].reset_index(drop=True)

    return merged_df, case_data


def run_district_predictions(root_dir: Path, pred_upto, cutoff_case, sampling_day, thresholds_df, granularity, region_name):
    with open(root_dir / "config/config_district.yaml", "r") as f:
        config = yaml.safe_load(f)

    (
        merged_df,
        case_data,
    ) = preprocess_case_and_weather_data(config, root_dir, pred_upto, sampling_day, granularity, region_name)

    # %% Model Estimation and Prediction
    """ NEGATIVE BINOMIAL REGRESSION """
    pred_upto_date = pred_upto
    to_date = pred_upto_date - pd.Timedelta(days=28)

    nbr_pred = NBR(config, merged_df, pred_upto_date).run_predictions()
    output_nbr = mergePredictionsThresholds(config, nbr_pred, thresholds_df=thresholds_df)
    output_nbr[output_nbr["startDatePredictedWeek"] <= pred_upto]

    """ TIME SERIES EXTRAPOLATION """
    pred_upto_date = cutoff_case.date() + pd.Timedelta(days=14)
    to_date = pred_upto_date - pd.Timedelta(days=14)

    if config["spatial_res"] != "district":
        logging.error("config['spatial_res'] should match 'district' column for time series extrapolation model to run.")
    else:
        tse_pred = TSE(config, case_data, pred_upto_date).run_predictions()
        output_tse = mergePredictionsThresholds(config, tse_pred, thresholds_df=thresholds_df)
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

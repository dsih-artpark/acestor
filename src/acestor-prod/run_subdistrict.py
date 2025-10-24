import pandas as pd
from datetime import datetime, timedelta, date
import utils
from copy import deepcopy
import models.tse as tse
import models.nbr as nbr
import yaml
import logging
import pickle

with open("config_subdistrict.yaml", "r") as f:
    config = yaml.safe_load(f)

with open("dumps/cutoffs.pkl", "rb") as f:
    cutoff, pred_upto, cutoff_case, cutoff_weather, prediction_dates = pickle.load(f)

with open("dumps/sampling_day.pkl", "rb") as f:
    sampling_day = pickle.load(f)


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
        print("WARNING: The list of dates is empty!")
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


# %% Load cases data
# case_data = utils.process_linelist_data(config)
# case_data = case_data[case_data['recordDate'] <= pd.Timestamp(2024, 7, 1)].reset_index(drop=True)
# case_data_ihip = utils.process_linelist_data_ihip(config)
# case_data_ihip = case_data_ihip[case_data_ihip['recordDate'] >= pd.Timestamp(2024, 7, 8)].reset_index(drop=True)
# case_data_ihip = case_data_ihip[case_data_ihip['recordDate'] <= cutoff_case].reset_index(drop=True)
# case_data = pd.concat([case_data, case_data_ihip]).reset_index(drop=True)

# case_data = case_data[case_data[config['spatial_res']].str.contains(config['spatial_res'])].reset_index(drop=True)
case_data = utils.process_case_data(config)

# %% Load weather data
weather_data = utils.process_weather_data(config)


# %% Merge cases and weather data
merged_df = utils.merge(config, case_data, weather_data)
# listValidDates = get_mondays(min(merged_df['recordDate']), pred_upto)
listValidDates = get_days(min(merged_df["recordDate"]), pred_upto, sampling_day=sampling_day)
merged_df = merged_df.sort_values([config["spatial_res"], "recordDate"]).reset_index(drop=True)
# merged_df = merged_df[merged_df['recordDate'].isin(listValidDates)].reset_index(drop=True)
merged_df = merged_df[merged_df["recordDate"].isin(listValidDates)].reset_index(drop=True)
# weather_cols = ['2mTemperature', 'totalPrecipitation', '2mDewpointTemperature']
# merged_df[weather_cols] = merged_df.groupby(config['spatial_res'])[weather_cols].ffill()


# %% Model Estimation and Prediction
""" NEGATIVE BINOMIAL REGRESSION """
pred_upto_date = pred_upto
to_date = pred_upto_date - pd.Timedelta(days=28)
nbr_pred = nbr.negative_binomial_regression(config, merged_df, predict_upto_date=pred_upto_date)
output_nbr = utils.mergePredictionsThresholds(config, case_data, nbr_pred, to_date, noOfDays=28)
output_nbr[output_nbr["startDatePredictedWeek"] <= pred_upto]
print(output_nbr)
# utils.save_prediction_output(output_nbr, config)


""" TIME SERIES EXTRAPOLATION """
pred_upto_date = cutoff_case.date() + pd.Timedelta(days=14)
to_date = pred_upto_date - pd.Timedelta(days=14)
if config["spatial_res"] != "district":
    logging.error("config['spatial_res'] should match 'district' column for time series extrapolation model to run.")
else:
    tse_pred = tse.linear_extrapolation(config, case_data, predict_upto_date=pred_upto_date)
    output_tse = utils.mergePredictionsThresholds(config, case_data, tse_pred, to_date, noOfDays=14)
    output_tse[output_tse["startDatePredictedWeek"] <= pd.Timestamp(pred_upto_date)]
    print(output_tse)
# merge the predictions of NBR and TSE
# utils.save_prediction_output(output_tse, config)

if config["spatial_res"] == "district":
    predictions_df0 = pd.concat([output_nbr, output_tse]).reset_index(drop=True)
else:
    predictions_df0 = output_nbr.reset_index(drop=True)

# Assign common threshold for a month
# predictions_df1 = utils.assignCommonThreshold(predictions_df0)
cols_for_grouping = [val for val in predictions_df0.columns if val not in ["prediction", "model"]]
predictions_df1 = predictions_df0.groupby(cols_for_grouping)["prediction"].max().reset_index()
if config["spatial_res"] == "district":
    predictions_df1.loc[:, "model"] = "ensembleModel"
else:
    predictions_df1.loc[:, "model"] = "negativeBinomialRegression"

# Assign risk classification zone
predictions_df2 = utils.classifyIntoZones(config, predictions_df1)

# Assign the max risk if there are different predictions from different models
# predictions_df = predictions_df2.groupby(['regionID', 'startDatePredictedWeek'])['predictionZone'].max().reset_index()
predictions_df = deepcopy(predictions_df2)
# predictions_df.loc[:, 'model'] = 'Ensemble Model'
predictions_df.loc[:, "dateOfComputingPrediction"] = datetime.now().strftime("%Y-%m-%d")
predictions_df.sort_values(by=["regionID", "startDatePredictedWeek"], ascending=[True, True], inplace=True)
predictions_df["predictionZone"] = predictions_df["predictionZone"].fillna(0)

listPredDates = list(predictions_df["startDatePredictedWeek"].unique())
listPredDates = [val for val in listPredDates if val >= pd.Timestamp.today().normalize()]
monthstring = get_month_year_range(listPredDates)
endString = pd.Timestamp.today().date().strftime(format="%Y%m%d")

# predictions_df.sort_values(by=['regionID', 'startDatePredictedWeek', 'model'], ascending=[True, False, True], inplace=True)
# predictions_df['predictionZone'] = predictions_df['predictionZone'].fillna(0)
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
    predictions_df_state = predictions_df.groupby(["startDatePredictedWeek", "model", "thresholdMethod"])["prediction"].sum().reset_index()
    predictions_df_state.loc[:, "dateOfComputingPrediction"] = datetime.now().strftime("%Y-%m-%d")
    # predictions_df_state.loc[:, 'model'] = 'negativeBinomialRegression'
    predictions_df_state.loc[:, "regionID"] = "state_29"
    predictions_df_state.loc[:, "predictionZone"] = 0
    predictions_df_state = predictions_df_state[cols_of_interest]
    predictions_df_state.drop_duplicates(inplace=True)
    predictions_df_state.to_csv(f"results/Predictions_{monthstring}_Karnataka_{endString}.csv", index=False)

# %% To do:

# Add both historical and prevNweeks thresholding in utils.mu_sigma()
# Add predZone=0 if latest data is not available for a regionID
# Add predZone=-1 if prediction is not applicable to regionID
# Make automation/running of code compatible with multiple spatial resolutions in config and concat all spatial resolution in one dataframe
# Add summary statistics of prediction output and visualizations including risk maps
# Run final validation checks of predicted output
# Add monthly model evaluation module.

# -*- coding: utf-8 -*-
"""
Created on Mon May 26 04:06:25 2025

@author: USER
"""

from datetime import datetime, timedelta
import utils
import yaml
from pathlib import Path
import logging

logger = logging.getLogger("identify-cutoff-dates")


def estimate_cutoff_date(*, df, p=10):
    """Estimate the cutoff date based on the case and weather data"""
    summary_df = df.groupby("recordDate").size().reset_index(name="no_of_districts")
    all_dates = list(df["recordDate"].unique())
    all_dates.sort()
    all_dates = all_dates[::-1]
    cutoff = None
    for thisdate in all_dates:
        if list(summary_df[summary_df["recordDate"] == thisdate]["no_of_districts"])[0] >= p:
            cutoff = thisdate
            break
    return cutoff


def identify_cutoff_dates(root_dir: Path, granularity, region_name):
    with open(root_dir / "config/config_district.yaml", "r") as f:
        config = yaml.safe_load(f)

    case_data = utils.process_case_data(config, root_dir, granularity)
    weather_data = utils.process_weather_data(config, root_dir, granularity, region_name)

    cutoff_case = estimate_cutoff_date(df=case_data, p=10)
    cutoff_weather = estimate_cutoff_date(df=weather_data, p=25)

    if cutoff_case < (cutoff_weather + timedelta(28)):
        cutoff = cutoff_case
        pred_upto = cutoff_weather + timedelta(28)
    else:
        cutoff = cutoff_weather + timedelta(28)
        pred_upto = cutoff

    prediction_dates = [(pred_upto - timedelta(days=(val) * 7)).date() for val in range(int((pred_upto - cutoff).days / 7))][::-1][-4:]
    prediction_dates = [datetime.strftime(val, format="%Y-%m-%d") for val in prediction_dates]
    prediction_dates_string = ", ".join(prediction_dates)

    logger.info(f"Useful case data is available up to: {cutoff_case.date()}")
    logger.info(f"Useful weather data is available up to: {cutoff_weather.date()}")
    logger.info(f"Model can be trained using data up to: {cutoff.date()}")
    logger.info(f"Predictions can be made up to: {pred_upto.date()}")

    if len(prediction_dates) > 0:
        logger.info(f"Prediction(s) can be made for: {prediction_dates_string}")
    else:
        logger.info(f"Weather data is not sufficient to make predictions! It is available up to {cutoff_weather.date()}")

    # with open("dumps/cutoffs.pkl", "wb") as f:
    #     pickle.dump([cutoff, pred_upto, cutoff_case, cutoff_weather, prediction_dates], f)

    return cutoff, pred_upto, cutoff_case, cutoff_weather, prediction_dates


if __name__ == "__main__":
    identify_cutoff_dates(root_dir=Path("."))

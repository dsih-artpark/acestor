import logging
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from utils import retNAfilledDF


class TSE:
    def __init__(self, config, data, predict_upto_date=None):
        self.config = config
        self.data = data
        self.predict_upto_date = predict_upto_date
        self.to_date = predict_upto_date - pd.Timedelta(days=14)

    def equivCase(self, x1, x2):
        return x1 if x2 is False else -999

    def countNaN(self, x):
        noOfNaN = len([val for val in x if val == -999])
        return np.nan if (noOfNaN > 2) else 0

    def processDistrict(self, config, df, predict_upto_date=None):
        df.loc[:, "IsNaN"] = pd.isna(df["case"])
        df.loc[:, "equivCase"] = df[["case", "IsNaN"]].apply(lambda x: self.equivCase(x.iloc[0], x.iloc[1]), axis=1)
        df.loc[:, "CountNaN"] = df["equivCase"].rolling(window=4).apply(self.countNaN)
        df.loc[:, "equivCase"] = df["equivCase"].apply(lambda x: x if x != -999 else 0)
        df.loc[:, "Avg"] = df["equivCase"].rolling(window=4).mean()
        df.loc[:, "4wMovingAvg"] = df[["CountNaN", "Avg"]].apply(lambda x: x.iloc[0] + x.iloc[1], axis=1)
        df["MaxCaseMonthlyHistorical"] = df.groupby(["region_id", "recordMonth"])["equivCase"].transform("max")
        df = df[~df["recordYear"].isin(config["years_to_exclude"])]
        df = df[(df["recordDate"] <= pd.to_datetime(predict_upto_date))]
        df.sort_values(by="recordDate", inplace=True)
        df = df.reset_index(drop=True)
        return df

    def run_predictions(self):
        # current_date = datetime.now().date()
        if self.predict_upto_date is None:
            # predict_upto_date = datetime.now().date()
            self.predict_upto_date = datetime(2025, 3, 10).date()
            to_date = self.predict_upto_date - pd.Timedelta(days=14)
        else:
            to_date = self.predict_upto_date - pd.Timedelta(days=14)

        df = retNAfilledDF(self.config, self.data, to_date=to_date)
        df.sort_values(by=["recordDate", "region_id"], ascending=[True, True], inplace=True)
        df = df.reset_index(drop=True)

        # Initialize an empty list to store the extrapolated rows
        extrapolated_rows = []

        # Initialize a list to track districts that do not meet the minimum condition
        districts_without_pred = []

        df.to_csv("datasets/debug/linear_extra.csv", index=False)

        for district_name, district_data in df.groupby("region_id"):
            district_data = self.processDistrict(self.config, district_data, self.predict_upto_date)

            # Get the last two dates and their 4-week moving averages
            last_date = district_data["recordDate"].iloc[-1]
            second_last_date = district_data["recordDate"].iloc[-2]

            # Check the condition for extrapolation
            if (to_date - last_date.to_pydatetime().date() <= timedelta(days=15)) and (
                to_date - second_last_date.to_pydatetime().date() <= timedelta(days=15)
            ):
                # Calculate the time difference and the rate of change in the 4wMovingAvg
                date_diff = (last_date - second_last_date).days
                avg_diff = district_data["4wMovingAvg"].iloc[-1] - district_data["4wMovingAvg"].iloc[-2]

                district_data.to_csv(f"datasets/debug/district_data_{district_name}.csv", index=False)

                # Extrapolate for the next two weeks
                for i in range(1, 3):
                    future_date = last_date + timedelta(days=7 * i)
                    future_prediction = district_data["4wMovingAvg"].iloc[-1] + (avg_diff / date_diff) * (7 * i)
                    future_prediction = max(future_prediction, 0)

                    # Get the historical maximum for the month
                    # month_max = district_data[district_data["recordMonth"] == future_date.month]["MaxCaseMonthlyHistorical"].iloc[0]

                    # Create a new row with the extrapolated data
                    new_row = {
                        "region_id": district_name,
                        "recordDate": future_date,
                        "prediction": future_prediction,
                        # "MaxCaseMonthlyHistorical": month_max,
                        "model": "timeSeriesExtrapolation",
                    }

                    # Append the new row to the list
                    extrapolated_rows.append(new_row)
            else:
                # Track districts that do not meet the condition
                districts_without_pred.append(district_name)

        # Create a DataFrame from the extrapolated rows
        extrapolated_df = pd.DataFrame(extrapolated_rows)

        # Print districts that did not meet the condition
        if districts_without_pred:
            logging.warning("Following districts do not have enough data for extrapolation:", districts_without_pred)
        else:
            logging.info("All districts met the condition for extrapolation.")

        return extrapolated_df

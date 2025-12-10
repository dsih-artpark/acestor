import logging
from copy import deepcopy
from datetime import datetime

import pandas as pd
import statsmodels.api as sm
from sklearn.preprocessing import MinMaxScaler, OneHotEncoder
from utils import retNAfilledDF


class NBR:
    def __init__(self, config, data, predict_upto_date=None):
        self.config = config
        self.data = data
        self.predict_upto_date = predict_upto_date
        self.to_date = predict_upto_date - pd.Timedelta(days=28)

    def lag(self, config, data_features):
        for lag in config["lag"]["lag_temp"]:
            col_name_temp = f"temp_lag_{lag}"
            data_features[[col_name_temp]] = data_features.groupby(config["spatial_res"])[["2mTemperature"]].shift(lag)
        for lag in config["lag"]["lag_rf"]:
            col_name_rainfall = f"rainfall_lag_{lag}"
            col_name_relative_humidity = f"relative_humidity_lag_{lag}"
            data_features[[col_name_rainfall, col_name_relative_humidity]] = data_features.groupby(config["spatial_res"])[
                ["totalPrecipitation", "2mDewpointTemperature"]
            ].shift(lag)
        return data_features

    def filter(self, config, data):
        data_features = data[
            config["data_features"] + ["rainfall_lag_4", "relative_humidity_lag_4", "temp_lag_12"] + [config["spatial_res"]]
        ]
        # # Apply lag
        # data_features = lag(config, data_features)
        # Filter out the years that are not required
        data_features_filtered = data_features[~data_features["recordYear"].isin(config["years_to_exclude"])]
        # Reset index
        data_features_filtered = data_features_filtered.dropna().reset_index(drop=True)
        return data_features_filtered

    def one_hot_encode(self, filtered_data, categorical_columns=["ISOWeek"]):
        encoder = OneHotEncoder(sparse_output=False)
        encoded_features = encoder.fit_transform(filtered_data[categorical_columns])
        encoded_df = pd.DataFrame(encoded_features, columns=encoder.get_feature_names_out(categorical_columns))
        return encoded_df

    def rescale(self, filtered_data, scaler=None):  # There are issues with this aspect
        input_features = filtered_data[
            [
                "rainfall_lag_4",
                "relative_humidity_lag_4",
                "temp_lag_12",
            ]
        ]
        # Drop columns with all NaN values
        input_features = input_features.dropna(axis=1, how="all")

        # Normalize the input features using Min-Max scaling
        if scaler is None:
            scaler = MinMaxScaler()
            scaled_features = pd.DataFrame(
                scaler.fit_transform(input_features),
                columns=[col for col in input_features.columns],
            )
        else:
            scaled_features = pd.DataFrame(
                scaler.transform(input_features),
                columns=[col for col in input_features.columns],
            )
        return scaled_features, scaler  # Why did we not return scaler before?

    def run_predictions(self):
        if self.predict_upto_date is None:
            # predict_upto_date = datetime.now().date()
            self.predict_upto_date = datetime(2025, 6, 2)
            to_date = self.predict_upto_date - pd.Timedelta(days=28)
        else:
            to_date = self.predict_upto_date - pd.Timedelta(days=28)

        merged_df0 = retNAfilledDF(self.config, self.data, to_date=self.predict_upto_date)

        merged_df0.to_csv("datasets/debug/return_nafilled_df.csv", index=False)
        merged_df0 = merged_df0[merged_df0["recordDate"] <= self.predict_upto_date].reset_index(drop=True)
        last_4_week_dates = merged_df0["recordDate"].sort_values().unique()[-4:]
        merged_df0.loc[merged_df0["recordDate"].isin(list(last_4_week_dates)), "ISOWeek"] = merged_df0[
            merged_df0["recordDate"].isin(list(last_4_week_dates))
        ]["recordDate"].apply(lambda x: datetime.isocalendar(x).week)

        merged_df0.to_csv("datasets/debug/merged_before_lag.csv")

        # Apply lag
        merged_df0 = self.lag(self.config, data_features=merged_df0)

        merged_df0.to_csv("datasets/debug/merged_after_lag.csv")

        # Filter out the last 4 weeks of data from merged_df
        merged_df_filtered = merged_df0[~merged_df0["recordDate"].isin(last_4_week_dates)]

        # Filter the data and split into training set
        train_data = deepcopy(merged_df_filtered)
        print(f"shape of train_data: {train_data.shape}")
        # Apply one-hot encoding and rescaling to the training data
        train_data.to_csv("datasets/train_data_debug.csv", index=False)
        filtered_train_data = filter(self.config, train_data)
        print(f"shape of filtered_train_data: {filtered_train_data.shape}")
        encoded_train_data = self.one_hot_encode(filtered_train_data)
        scaler = None
        scaled_train_features, scaler = self.rescale(filtered_train_data, scaler)
        train_features = pd.concat([scaled_train_features, encoded_train_data], axis=1)
        X_train = sm.add_constant(train_features)
        y_train = filtered_train_data["case"]

        X_train.to_csv("datasets/debug/x_train.csv", index=False)
        y_train.to_csv("datasets/debug/y_train.csv", index=False)

        # Fit Negative Binomial Regression model to the training data
        neg_binom_model = sm.GLM(y_train, X_train, family=sm.families.NegativeBinomial(alpha=1.0))
        neg_binom_results = neg_binom_model.fit(method="lbfgs")

        # Predict on the test set (last 4 weeks of weather data)
        # if to_date is None:
        #     to_date = datetime(2023, 5, 14) + timedelta(days=28)
        # merged_df = merged_df0[merged_df0['recordDate'] <= to_date].reset_index(drop=True)

        # Find the last 4 weeks of weather data
        # weatherDataColumns = ['district', 'totalPrecipitation', '2mDewpointTemperature', '2mTemperature',
        #                       'recordDate', 'recordYear', 'ISOWeek']
        # weather_data0 = merged_df0[weatherDataColumns]

        # test_data = weather_data0[weather_data0['recordDate'].isin(last_4_week_dates)].copy()
        # for col1, col2 in zip(["rainfall_lag_4", "relative_humidity_lag_4", "temp_lag_12"], ['totalPrecipitation', '2mDewpointTemperature', '2mTemperature']):
        #     test_data.loc[:, col1] = test_data[col2]

        test_data = merged_df0[merged_df0["recordDate"].isin(last_4_week_dates)].copy().reset_index(drop=True)
        test_data.to_csv("datasets/debug/test_data.csv", index=False)
        encoded_test_data = self.one_hot_encode(test_data)
        # DOUBT ON THE FOLLOWING: How do we ensure that the scaling here is same as in the train data?
        scaled_test_features, scaler = self.rescale(test_data, scaler)
        test_features = pd.concat([scaled_test_features, encoded_test_data], axis=1)
        X_test = sm.add_constant(test_features)

        # Check all available columns in the train data
        train_columns = set(X_train.columns)
        test_columns = set(X_test.columns)

        # Add missing columns in test data with 0 as the entry using .loc to avoid SettingWithCopyWarning
        missing_columns = train_columns - test_columns
        for col in missing_columns:
            X_test.loc[:, col] = 0

        # Sort input parameters to match the order of the columns in the train data
        column_order = X_train.columns
        X_test = X_test[column_order]
        y_pred = neg_binom_results.predict(X_test)

        y_pred_aligned = pd.Series(y_pred.values, index=test_data.index)

        # Merge predicted values with test_data based on index
        test_data_with_predictions = test_data.copy()
        test_data_with_predictions["prediction"] = y_pred_aligned
        test_data_with_predictions["recordDate"] = pd.to_datetime(test_data_with_predictions["recordDate"])
        test_data_with_predictions["model"] = "negativeBinomialRegression"
        test_data_with_predictions.reset_index(inplace=True, drop=True)

        # log model summary
        logging.info(neg_binom_results.summary())

        return test_data_with_predictions

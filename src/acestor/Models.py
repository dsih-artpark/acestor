import logging
from copy import deepcopy
from datetime import datetime

import pandas as pd
import statsmodels.api as sm
from sklearn.preprocessing import MinMaxScaler, OneHotEncoder


class Model:
    def __init__():
        pass

    def fit(self, x, y):
        pass

    def predict(self, x):
        pass


class NegativeBinomialRegression(Model):
    def __init__(self):
        super().__init__()
        self.model = None
        self.results = None

    def fit(self, X_train, y_train):
        self.model = sm.GLM(y_train, X_train, family=sm.families.NegativeBinomial(alpha=1.0))
        self.results = self.model.fit(method="lbfgs")
        return self.results

    def predict(self, X_test):
        return self.results.predict(X_test)

import numpy as np
import pandas as pd
from skopt.space import Integer

from evalml.model_family import ModelFamily
from evalml.pipelines.components.estimators import Estimator
from evalml.problem_types import ProblemTypes
from evalml.utils import import_or_raise, infer_feature_types


class ARIMARegressor(Estimator):
    """
    Autoregressive Integrated Moving Average Model.
    The three parameters (p, d, q) are the AR order, the degree of differencing, and the MA order.
    More information here: https://www.statsmodels.org/devel/generated/statsmodels.tsa.arima_model.ARIMA.html

    """
    name = "ARIMA Regressor"
    hyperparameter_ranges = {
        "p": Integer(0, 10),
        "d": Integer(0, 10),
        "q": Integer(0, 10),
    }
    model_family = ModelFamily.ARIMA
    supported_problem_types = [ProblemTypes.TIME_SERIES_REGRESSION]

    def __init__(self, date_column=None, trend='n', p=1, d=0, q=0,
                 random_seed=0, **kwargs):
        """
        Arguments:
            date_column (str): Specifies the name of the column in X that provides the datetime objects. Defaults to None.
            trend (str): Controls the deterministic trend. Options are ['n', 'c', 't', 'ct'] where 'c' is a constant term,
                't' indicates a linear trend, and 'ct' is both. Can also be an iterable when defining a polynomial, such
                as [1, 1, 0, 1].
            p (int or list(int)): Autoregressive order.
            d (int): Differencing degree.
            q (int or list(int)): Moving Average order.
        """

        order = (p, d, q)
        dummy_dates = pd.DatetimeIndex(['2000-01-01', '2000-01-02', '2000-01-03', '2000-01-04'], freq='D', name=date_column)
        parameters = {'order': order,
                      'trend': trend,
                      'dates': dummy_dates}

        parameters.update(kwargs)

        p_error_msg = "ARIMA is not installed. Please install using `pip install statsmodels`."

        arima = import_or_raise("statsmodels.tsa.arima.model", error_msg=p_error_msg)

        try:
            sum_p = sum(p) if isinstance(p, list) else p
            sum_q = sum(q) if isinstance(q, list) else q
            arima.ARIMA(endog=np.zeros(sum_p + d + sum_q + 1), **parameters)
        except TypeError:
            raise TypeError("Unable to instantiate ARIMA due to an unexpected argument")
        parameters.update({'p': p,
                           'd': d,
                           'q': q})

        super().__init__(parameters=parameters,
                         component_obj=None,
                         random_seed=random_seed)

    def _get_dates_fit(self, X, y):
        date_col = None
        y_index_type = infer_feature_types(pd.Series(y.index)).logical_type.type_string
        if y_index_type == 'datetime':
            date_col = y.index
        if X is not None:
            X_index_type = infer_feature_types(pd.Series(X.index)).logical_type.type_string
            if self.parameters["dates"].name in X.columns:
                date_col = X.pop(self.parameters["dates"].name)
            elif X_index_type == 'datetime':
                date_col = X.index

        if date_col is None:
            msg = "ARIMA regressor requires input data X to have a datetime column specified by the 'date_column' parameter. " \
                  "If not it will look for the datetime column in the index of X or y."
            raise ValueError(msg)
        return date_col, X

    def _get_dates_predict(self, X, y):
        date_col = None
        if y is not None:
            y_index_type = infer_feature_types(pd.Series(y.index)).logical_type.type_string
            if y_index_type == 'datetime':
                date_col = y.index
        if X is not None:
            X_index_type = infer_feature_types(pd.Series(X.index)).logical_type.type_string
            if self.parameters["dates"].name in X.columns:
                date_col = X.pop(self.parameters["dates"].name)
            elif X_index_type == 'datetime':
                date_col = X.index

        if date_col is None:
            msg = "ARIMA regressor requires input data X to have a datetime column specified by the 'date_column' parameter. " \
                  "If not it will look for the datetime column in the index of X or y."
            raise ValueError(msg)
        return date_col, X

    def _match_indices(self, X, y, date_col):
        if X is not None:
            X.index = date_col
        if y is not None:
            y.index = date_col
        return X, y

    def fit(self, X, y=None):
        if y is None:
            raise ValueError('ARIMA Regressor requires y as input.')

        p_error_msg = "ARIMA is not installed. Please install using `pip install statsmodels`."
        arima = import_or_raise("statsmodels.tsa.arima.model", error_msg=p_error_msg)

        X, y = self._manage_woodwork(X, y)
        dates, X = self._get_dates_fit(X, y)
        dates = pd.to_datetime(dates)
        dates.freq = pd.infer_freq(dates)
        dates = pd.DatetimeIndex(dates, freq=dates.freq)
        X, y = self._match_indices(X, y, dates)
        new_params = {}
        for key, val in self.parameters.items():
            if key not in ['p', 'd', 'q', 'dates']:
                new_params[key] = val
        if X is not None:
            arima_with_data = arima.ARIMA(endog=y, exog=X, dates=dates, **new_params)
        else:
            arima_with_data = arima.ARIMA(endog=y, dates=dates, **new_params)

        self._component_obj = arima_with_data.fit()
        return self

    def predict(self, X, y=None):
        X, y = self._manage_woodwork(X, y)
        dates, X = self._get_dates_predict(X, y)
        X, y = self._match_indices(X, y, dates)
        start = pd.to_datetime(dates).min()
        end = pd.to_datetime(dates).max()
        if X is not None:
            y_pred = self._component_obj.predict(start=start, end=end, exog=X)
        else:
            y_pred = self._component_obj.predict(start=start, end=end)
        return infer_feature_types(y_pred)

    @property
    def feature_importance(self):
        """
        Returns array of 0's with a length of 1 as feature_importance is not defined for ARIMA regressor.
        """
        return np.zeros(1)

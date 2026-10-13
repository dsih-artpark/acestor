"""Regularised Negative Binomial Regression model (issue #155).

This ports the reference ``nbr_regularised_realtime`` workflow into Acestor's
weekly prediction pipeline. Case lags are transformed with ``log1p``, weather
lags are standardised, and future weeks are forecast recursively while the
weather feature vector stays fixed at the forecast origin.

Enable it by listing ``nbr_regularised`` in ``model.models``. Settings are
optional under ``model_configs.nbr_regularised``; see ``_params`` for defaults.
"""

from __future__ import annotations

import hashlib
import json
import logging
import warnings
from dataclasses import dataclass
from typing import Any

import numpy as np
import optuna
import pandas as pd
import statsmodels.api as sm
from sklearn.metrics import mean_squared_error
from sklearn.preprocessing import StandardScaler
from statsmodels.discrete.discrete_model import NegativeBinomial
from statsmodels.tools.sm_exceptions import ConvergenceWarning

from pipelines.dengue.lib.models import ModelContext, register
from pipelines.dengue.lib.models import _tuning
from pipelines.dengue.lib.models._shared import _lag, recursive_forecast
from pipelines.dengue.lib.zones import ret_na_filled_df

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _NBParams:
    use_reg: bool = True
    reg_method: str = "l1"
    reg_alpha: float = 0.01
    maxiter: int = 1000
    trim_mode: str = "size"
    qc_tol: float = 0.05
    penalize_intercept: bool = False
    penalize_alpha: bool = False


def _params(
    raw: dict[str, Any], *, default_n_trials: int = 100
) -> tuple[_NBParams, dict[str, Any]]:
    reg = raw.get("regularization", {}) or {}
    tuning = raw.get("tuning", {}) or {}
    params = _NBParams(
        use_reg=bool(reg.get("use_reg", True)),
        reg_method=str(reg.get("reg_method", "l1")),
        reg_alpha=float(reg.get("reg_alpha", 0.01)),
        maxiter=int(reg.get("maxiter", 1000)),
        trim_mode=str(reg.get("trim_mode", "size")),
        qc_tol=float(reg.get("qc_tol", 0.05)),
        penalize_intercept=bool(reg.get("penalize_intercept", False)),
        penalize_alpha=bool(reg.get("penalize_alpha", False)),
    )
    tuning_params = {
        "low": float(tuning.get("reg_alpha_low", 1e-4)),
        "high": float(tuning.get("reg_alpha_high", 1.0)),
        "log": bool(tuning.get("reg_alpha_log", True)),
        "n_trials": int(
            tuning.get("n_trials", raw.get("n_trials", default_n_trials))
        ),
        "suppress_warnings": bool(tuning.get("suppress_fit_warnings", True)),
        "folds": tuning.get("folds"),
    }
    if params.reg_alpha < 0:
        raise ValueError(
            "model_configs.nbr_regularised.regularization.reg_alpha must be >= 0"
        )
    if tuning_params["low"] <= 0 or tuning_params["high"] <= tuning_params["low"]:
        raise ValueError("nbr_regularised tuning bounds must satisfy 0 < low < high")
    if tuning_params["n_trials"] < 1:
        raise ValueError("nbr_regularised tuning.n_trials must be >= 1")
    return params, tuning_params


def _feature_columns(
    ctx: ModelContext,
) -> tuple[list[int], list[int], list[int], list[int], list[str]]:
    cfg = ctx.cfg
    lag_cases = list(cfg.lag_cases) or [1, 2, 3, 4, 5]
    lag_temp = list(cfg.lag_temp)
    lag_rainfall = list(cfg.lag_rainfall)
    lag_humidity = list(cfg.lag_humidity)
    cols = (
        [f"case_lag_{lag}" for lag in lag_cases]
        + [f"temp_lag_{lag}" for lag in lag_temp]
        + [f"rainfall_lag_{lag}" for lag in lag_rainfall]
        + [f"relative_humidity_lag_{lag}" for lag in lag_humidity]
    )
    if not cols:
        raise ValueError("nbr_regularised requires at least one case or weather lag")
    return lag_cases, lag_temp, lag_rainfall, lag_humidity, cols


def _fit_estimator(X: pd.DataFrame, y: np.ndarray, params: _NBParams, alpha: float):
    model = NegativeBinomial(y, X)
    if not params.use_reg:
        return model.fit(maxiter=params.maxiter, disp=0)

    penalty = np.full(len(model.exog_names), alpha, dtype=float)
    for i, name in enumerate(model.exog_names):
        if name == "const" and not params.penalize_intercept:
            penalty[i] = 0.0
        elif name == "alpha" and not params.penalize_alpha:
            penalty[i] = 0.0
    return model.fit_regularized(
        method=params.reg_method,
        alpha=penalty,
        maxiter=params.maxiter,
        disp=0,
        trim_mode=params.trim_mode,
        qc_tol=params.qc_tol,
    )


def _fit_with_status(
    X: pd.DataFrame,
    y: np.ndarray,
    params: _NBParams,
    alpha: float,
) -> tuple[Any, bool | None, bool]:
    """Fit and report optimizer convergence separately from the L1 QC check."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ConvergenceWarning)
        result = _fit_estimator(X, y, params, alpha)
    retvals = getattr(result, "mle_retvals", {}) or {}
    converged = retvals.get("converged")
    qc_passed = not any(
        issubclass(item.category, ConvergenceWarning)
        and str(item.message).startswith(
            (
                "QC check did not pass",
                "Could not trim params automatically due to failed QC check",
            )
        )
        for item in caught
    )
    return result, converged, qc_passed


def _transform(
    X: pd.DataFrame,
    *,
    feature_cols: list[str],
    case_cols: list[str],
    weather_cols: list[str],
    scaler: StandardScaler | None = None,
    fit_scaler: bool = False,
) -> tuple[pd.DataFrame, StandardScaler | None]:
    out = X[feature_cols].astype(float).copy()
    if case_cols:
        out.loc[:, case_cols] = np.log1p(out[case_cols])
    if weather_cols:
        if scaler is None and fit_scaler:
            scaler = StandardScaler()
            out.loc[:, weather_cols] = scaler.fit_transform(out[weather_cols])
        elif scaler is not None:
            out.loc[:, weather_cols] = scaler.transform(out[weather_cols])
    return out, scaler


def _design_matrix(X: pd.DataFrame) -> pd.DataFrame:
    return sm.add_constant(X, prepend=False, has_constant="add")


class _RecursiveNB:
    """Adapt the fitted statsmodels result to recursive_forecast's predict API."""

    def __init__(self, result, feature_cols, case_cols, weather_cols, scaler):
        self.result = result
        self.feature_cols = feature_cols
        self.case_cols = case_cols
        self.weather_cols = weather_cols
        self.scaler = scaler

    def predict(self, rows):
        X = pd.DataFrame(rows, columns=self.feature_cols)
        transformed, _ = _transform(
            X,
            feature_cols=self.feature_cols,
            case_cols=self.case_cols,
            weather_cols=self.weather_cols,
            scaler=self.scaler,
        )
        return np.asarray(self.result.predict(exog=_design_matrix(transformed)))


def _training_data(
    ctx: ModelContext, feature_cols: list[str], lag_args
) -> tuple[pd.DataFrame, list[pd.Timestamp], pd.DataFrame]:
    lag_cases, lag_temp, lag_rainfall, lag_humidity = lag_args
    df = ret_na_filled_df(
        ctx.merged_df, spatial_col=ctx.cfg.spatial_res, to_date=ctx.pred_upto
    )
    df["recordDate"] = pd.to_datetime(df["recordDate"])
    df = df[df["recordDate"] <= ctx.pred_upto].copy()
    df = df.sort_values([ctx.cfg.spatial_res, "recordDate"]).reset_index(drop=True)
    df = _lag(df, ctx.cfg.spatial_res, lag_temp, lag_rainfall, lag_humidity, lag_cases)
    future_dates = sorted(pd.to_datetime(d) for d in (ctx.prediction_dates or []))
    available_dates = sorted(df["recordDate"].dropna().unique())
    if not future_dates:
        future_dates = available_dates[-4:]
    # Match the pipeline's target grid to available dates and retain only
    # actual future rows; weather at the last observed origin remains usable.
    future_dates = [d for d in future_dates if d in set(available_dates)]
    if not future_dates:
        future_dates = available_dates[-4:]

    train = df[~df["recordDate"].isin(future_dates)].copy()
    if ctx.cfg.years_to_include:
        train = train[train["recordYear"].isin(ctx.cfg.years_to_include)]
    if ctx.cfg.years_to_exclude:
        train = train[~train["recordYear"].isin(ctx.cfg.years_to_exclude)]
    missing = [col for col in feature_cols if col not in train.columns]
    if missing:
        raise ValueError(f"nbr_regularised lag columns missing from input: {missing}")
    train = train.dropna(subset=feature_cols + ["case"]).sort_values("recordDate")
    return train.reset_index(drop=True), future_dates, df


def _tuning_fingerprint(
    ctx: ModelContext,
    train_max_date: str,
    raw: dict[str, Any],
    feature_cols: list[str],
) -> str:
    base = _tuning.compute_fingerprint(ctx.cfg, train_max_date)
    payload = {
        "base": base,
        "features": feature_cols,
        "regularization": raw.get("regularization", {}),
        "tuning": raw.get("tuning", {}),
    }
    encoded = json.dumps(payload, sort_keys=True, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def _tune_alpha(
    train: pd.DataFrame,
    feature_cols: list[str],
    case_cols: list[str],
    weather_cols: list[str],
    params: _NBParams,
    tuning: dict[str, Any],
    seed: int,
) -> tuple[float, float, list[int]]:
    dates = np.sort(train["recordDate"].unique())
    folds = []
    configured_folds = tuning.get("folds")
    if configured_folds:
        for fold in configured_folds:
            train_end = int(fold["train_end"])
            val_end = min(int(fold["val_end"]), len(dates))
            if train_end < 1 or val_end <= train_end:
                raise ValueError(f"Invalid nbr_regularised tuning fold: {fold}")
            folds.append((dates[:train_end], dates[train_end:val_end]))
    else:
        n_folds = min(5, max(1, len(dates) // 8))
        fold_size = len(dates) // (n_folds + 1)
        if fold_size < 1:
            raise ValueError(
                "nbr_regularised tuning needs at least two distinct training dates"
            )
        for i in range(n_folds):
            train_end = fold_size * (i + 1)
            val_end = min(train_end + fold_size, len(dates))
            if val_end > train_end:
                folds.append((dates[:train_end], dates[train_end:val_end]))

    def objective(trial):
        alpha = trial.suggest_float(
            "reg_alpha", tuning["low"], tuning["high"], log=tuning["log"]
        )
        rmses = []
        qc_warning_folds = []
        for fold_id, (train_dates, val_dates) in enumerate(folds):
            fold_train = train[train["recordDate"].isin(train_dates)]
            fold_val = train[train["recordDate"].isin(val_dates)]
            X_train, scaler = _transform(
                fold_train,
                feature_cols=feature_cols,
                case_cols=case_cols,
                weather_cols=weather_cols,
                fit_scaler=True,
            )
            X_val, _ = _transform(
                fold_val,
                feature_cols=feature_cols,
                case_cols=case_cols,
                weather_cols=weather_cols,
                scaler=scaler,
            )
            try:
                with warnings.catch_warnings():
                    if tuning["suppress_warnings"]:
                        warnings.simplefilter("ignore", RuntimeWarning)
                    result, converged, qc_passed = _fit_with_status(
                        _design_matrix(X_train),
                        fold_train["case"].to_numpy(),
                        params,
                        alpha,
                    )
                if converged is not True:
                    trial.set_user_attr("failed_fold", fold_id)
                    reason = (
                        "optimizer did not converge"
                        if converged is False
                        else "optimizer convergence status was unavailable"
                    )
                    trial.set_user_attr("failure_reason", reason)
                    return float("inf")
                if not qc_passed:
                    qc_warning_folds.append(fold_id)
                pred = np.asarray(
                    result.predict(exog=_design_matrix(X_val)), dtype=float
                )
                if not np.all(np.isfinite(pred)):
                    trial.set_user_attr("failed_fold", fold_id)
                    trial.set_user_attr(
                        "failure_reason", "validation predictions were non-finite"
                    )
                    return float("inf")
                rmses.append(
                    float(np.sqrt(mean_squared_error(fold_val["case"], pred)))
                )
            except Exception as exc:
                trial.set_user_attr("failed_fold", fold_id)
                trial.set_user_attr("failure_reason", str(exc))
                return float("inf")
        trial.set_user_attr("qc_warning_folds", qc_warning_folds)
        return float(np.mean(rmses)) if rmses else float("inf")

    study = optuna.create_study(
        direction="minimize", sampler=optuna.samplers.TPESampler(seed=seed)
    )
    study.optimize(objective, n_trials=tuning["n_trials"], show_progress_bar=False)
    failed_trials = [trial for trial in study.trials if not np.isfinite(trial.value)]
    if failed_trials:
        nonconverged = sum(
            trial.user_attrs.get("failure_reason") in {
                "optimizer did not converge",
                "optimizer convergence status was unavailable",
            }
            for trial in failed_trials
        )
        log.warning(
            "NBR regularised tuning status: %d/%d trials failed; %d had a "
            "optimizer fold without confirmed convergence",
            len(failed_trials),
            len(study.trials),
            nonconverged,
        )
    if not np.isfinite(study.best_value):
        raise RuntimeError(
            "nbr_regularised tuning found no hyperparameter trial whose folds "
            "converged with finite validation predictions"
        )
    best_trial = study.best_trial
    return (
        float(best_trial.params["reg_alpha"]),
        float(best_trial.value),
        list(best_trial.user_attrs.get("qc_warning_folds", [])),
    )


def _resolve_alpha(
    ctx: ModelContext,
    train: pd.DataFrame,
    feature_cols: list[str],
    case_cols: list[str],
    weather_cols: list[str],
    params: _NBParams,
    tuning: dict[str, Any],
) -> float:
    raw = ctx.model_params or {}
    alpha = params.reg_alpha
    if not params.use_reg or ctx.artifacts is None:
        return alpha

    model_name = "nbr_regularised"
    region = getattr(ctx.cfg, "spatial_res", "") or ""
    cached = _tuning.load_cached_params(ctx.artifacts, model_name, region)
    train_max = str(train["recordDate"].max().date())
    fingerprint = _tuning_fingerprint(ctx, train_max, raw, feature_cols)
    saved_fingerprint = _tuning.load_fingerprint(ctx.artifacts, model_name, region)

    if ctx.cfg.tune == "never":
        if cached is None:
            raise FileNotFoundError(
                f"nbr_regularised [{region or '?'}]: tune='never' but no cached "
                f"hyperparameters found at {_tuning.hp_cache_path(model_name, region)!r}. "
                "Prime the cache with a run using tune=true."
            )
        return float(cached["params"]["reg_alpha"])

    if cached is not None and ctx.cfg.tune is not True and saved_fingerprint == fingerprint:
        return float(cached["params"]["reg_alpha"])

    if ctx.cfg.tune is not True and cached is not None and saved_fingerprint != fingerprint:
        log.info("NBR regularised [%s]: cached tuning is stale; retuning", region or "?")
    alpha, rmse, qc_warning_folds = _tune_alpha(
        train,
        feature_cols,
        case_cols,
        weather_cols,
        params,
        tuning,
        seed=int(raw.get("random_state", 42)),
    )
    if qc_warning_folds:
        log.warning(
            "NBR regularised [%s]: selected tuning trial had QC convergence "
            "warnings in fold(s) %s (optimizer converged)",
            region or "?",
            qc_warning_folds,
        )
    else:
        log.info(
            "NBR regularised [%s]: selected tuning trial converged in all folds",
            region or "?",
        )
    _tuning.save_params(
        ctx.artifacts,
        model_name,
        region,
        {"reg_alpha": alpha},
        rmse=rmse,
        n_trials=tuning["n_trials"],
        tuned_at=pd.Timestamp.now().strftime("%Y-%m-%d"),
    )
    _tuning.save_fingerprint(ctx.artifacts, model_name, region, fingerprint)
    return alpha


def regularised_nbr_regression(ctx: ModelContext) -> pd.DataFrame:
    lag_cases, lag_temp, lag_rainfall, lag_humidity, feature_cols = _feature_columns(ctx)
    train, future_dates, df0 = _training_data(
        ctx, feature_cols, (lag_cases, lag_temp, lag_rainfall, lag_humidity)
    )
    if train.empty:
        log.warning("NBR regularised: training data empty after filtering")
        return pd.DataFrame()
    if not future_dates:
        log.warning("NBR regularised: no forecast dates available")
        return pd.DataFrame()

    params, tuning = _params(
        ctx.model_params or {}, default_n_trials=ctx.cfg.n_trials
    )
    case_cols = [c for c in feature_cols if c.startswith("case_lag_")]
    weather_cols = [c for c in feature_cols if c not in case_cols]
    alpha = _resolve_alpha(
        ctx, train, feature_cols, case_cols, weather_cols, params, tuning
    )
    X_train, scaler = _transform(
        train,
        feature_cols=feature_cols,
        case_cols=case_cols,
        weather_cols=weather_cols,
        fit_scaler=True,
    )
    result, converged, qc_passed = _fit_with_status(
        _design_matrix(X_train), train["case"].to_numpy(), params, alpha
    )
    log.info(
        "NBR regularised final fit status: optimizer_converged=%s qc_passed=%s",
        converged,
        qc_passed,
    )
    if converged is not True:
        reason = (
            "did not converge"
            if converged is False
            else "did not report its convergence status"
        )
        log.error(
            "NBR regularised final model fit %s; excluding this model from the "
            "forecast",
            reason,
        )
        return pd.DataFrame()
    if not qc_passed:
        log.warning(
            "NBR regularised final fit optimizer converged, but the statsmodels "
            "QC convergence check emitted a warning"
        )

    recursive_model = _RecursiveNB(
        result, feature_cols, case_cols, weather_cols, scaler
    )
    predictions, _ = recursive_forecast(
        recursive_model,
        df0,
        spatial_col=ctx.cfg.spatial_res,
        feature_cols=feature_cols,
        lag_cases=lag_cases,
        future_dates=future_dates,
        clip_multiplier=ctx.cfg.clip_multiplier,
        train_max=float(train["case"].max()),
        debug=False,
        freeze_weather_at_origin=True,
    )
    if predictions.empty:
        log.warning("NBR regularised: no regions have valid lag features")
        return pd.DataFrame()
    predictions["model"] = "negativeBinomialRegressionRegularised"
    return predictions.drop(columns=["was_clipped"], errors="ignore").reset_index(drop=True)


@register("nbr_regularised")
class RegularisedNBRModel:
    def predict(self, ctx: ModelContext) -> pd.DataFrame:
        return regularised_nbr_regression(ctx)

    def threshold_to_date(self, ctx: ModelContext) -> pd.Timestamp:
        return ctx.cutoff_case

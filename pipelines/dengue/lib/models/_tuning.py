"""Optuna hyperparameter tuning + JSON cache for RF and XGB models.

Fold logic and search spaces ported from vbd-modelbench tune_rf_cv / tune_xgb_cv.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

import numpy as np
import optuna
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_squared_error
from xgboost import XGBRegressor

optuna.logging.set_verbosity(optuna.logging.WARNING)

log = logging.getLogger(__name__)


def hp_cache_path(model_name: str) -> str:
    return f"hp/{model_name}_best_params.json"


def fingerprint_path(model_name: str) -> str:
    return f"hp/{model_name}_fingerprint.json"


def compute_fingerprint(cfg: Any, train_max_date: str) -> str:
    """Return a short hash of the inputs that would invalidate cached hyperparams.

    Covers lag windows, feature list, year filters, and the training data cutoff.
    If any of these change, cached params from a previous run are stale.
    """
    payload = {
        "lag_temp": sorted(cfg.lag_temp),
        "lag_rainfall": sorted(cfg.lag_rainfall),
        "lag_humidity": sorted(cfg.lag_humidity),
        "lag_cases": sorted(cfg.lag_cases),
        "data_features": sorted(cfg.data_features),
        "years_to_exclude": sorted(cfg.years_to_exclude),
        "years_to_include": sorted(cfg.years_to_include),
        "train_max_date": train_max_date,
    }
    raw = json.dumps(payload, sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def save_fingerprint(storage: Any, model_name: str, fingerprint: str) -> None:
    storage.write_text(
        json.dumps({"fingerprint": fingerprint}),
        fingerprint_path(model_name),
    )


def load_fingerprint(storage: Any, model_name: str) -> str | None:
    try:
        data = storage.read_json(fingerprint_path(model_name))
        return data.get("fingerprint")
    except (FileNotFoundError, KeyError):
        return None


def check_fingerprint(
    storage: Any,
    model_name: str,
    cfg: Any,
    train_max_date: str,
    _log: Any,
) -> bool:
    """Return True if cached params are valid for the current config.

    Logs a warning if the fingerprint is missing or stale so the operator knows
    to set tune=true to regenerate hyperparameters.
    """
    current = compute_fingerprint(cfg, train_max_date)
    saved = load_fingerprint(storage, model_name)
    if saved is None:
        _log.warning(
            "%s: no tuning fingerprint found alongside cached params — "
            "if you changed lag config or features, set tune=true to retune",
            model_name.upper(),
        )
        return True  # allow cached params through; operator must opt-in to retune
    if saved != current:
        _log.warning(
            "%s: tuning fingerprint mismatch — cached params may be stale "
            "(config changed since last tune). Set tune=true to regenerate.",
            model_name.upper(),
        )
        return False
    return True


def load_cached_params(storage: Any, model_name: str) -> dict | None:
    try:
        return storage.read_json(hp_cache_path(model_name))
    except (FileNotFoundError, KeyError):
        return None


def save_params(
    storage: Any,
    model_name: str,
    params: dict,
    *,
    rmse: float,
    n_trials: int,
    tuned_at: str,
) -> None:
    payload = {
        "tuned_at": tuned_at,
        "n_trials": n_trials,
        "best_rmse": round(float(rmse), 4),
        "params": params,
    }
    storage.write_text(json.dumps(payload, indent=2), hp_cache_path(model_name))


def _make_folds(n: int, n_folds: int = 5) -> list[dict]:
    """Expanding-window folds over n time steps (mirrors vbd-modelbench fold logic).

    Each fold trains on everything up to train_end and validates on the next block.
    train_end and val_end are 1-based indices into the sorted time axis.
    """
    fold_size = n // (n_folds + 1)
    folds = []
    for i in range(n_folds):
        train_end = fold_size * (i + 1)
        val_end = train_end + fold_size
        folds.append({"train_end": train_end, "val_end": min(val_end, n)})
    return folds


def tune_rf(
    X_train: np.ndarray, y_train: np.ndarray, *, n_trials: int
) -> tuple[dict, float]:
    """Optuna search for RF hyperparameters. Returns (best_params, best_rmse).

    Search space matches vbd-modelbench tune_rf_cv.
    """
    n = len(X_train)
    folds = _make_folds(n)

    def objective(trial):
        params = {
            "n_estimators": trial.suggest_int("n_estimators", 100, 800, step=50),
            "max_depth": trial.suggest_int("max_depth", 5, 40),
            "min_samples_leaf": trial.suggest_int("min_samples_leaf", 1, 20),
            "max_features": trial.suggest_categorical(
                "max_features", ["sqrt", "log2", 0.3, 0.5, 0.7, 1.0]
            ),
        }
        fold_rmses = []
        for fold in folds:
            t_end, v_end = fold["train_end"], fold["val_end"]
            X_t, y_t = X_train[:t_end], y_train[:t_end]
            X_v, y_v = X_train[t_end:v_end], y_train[t_end:v_end]
            if len(X_t) == 0 or len(X_v) == 0:
                continue
            m = RandomForestRegressor(**params, random_state=42, n_jobs=-1)
            m.fit(X_t, y_t)
            preds = np.maximum(0.0, m.predict(X_v))
            fold_rmses.append(mean_squared_error(y_v, preds) ** 0.5)
        return float(np.mean(fold_rmses)) if fold_rmses else float("inf")

    study = optuna.create_study(direction="minimize")
    study.optimize(objective, n_trials=n_trials, show_progress_bar=False)
    return study.best_params, study.best_value


def tune_xgb(
    X_train: np.ndarray, y_train: np.ndarray, *, n_trials: int
) -> tuple[dict, float]:
    """Optuna search for XGB hyperparameters. Returns (best_params, best_rmse).

    Search space matches vbd-modelbench tune_xgb_cv.
    """
    n = len(X_train)
    folds = _make_folds(n)

    def objective(trial):
        params = {
            "n_estimators": trial.suggest_int("n_estimators", 400, 2000, step=200),
            "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.1, log=True),
            "max_depth": trial.suggest_int("max_depth", 3, 6),
            "min_child_weight": trial.suggest_int("min_child_weight", 5, 20),
            "subsample": trial.suggest_float("subsample", 0.6, 0.9),
            "colsample_bytree": trial.suggest_float("colsample_bytree", 0.6, 0.9),
            "gamma": trial.suggest_float("gamma", 0.0, 2.0),
            "reg_alpha": trial.suggest_float("reg_alpha", 0.0, 2.0),
            "reg_lambda": trial.suggest_float("reg_lambda", 2.0, 10.0),
        }
        fold_rmses = []
        for fold in folds:
            t_end, v_end = fold["train_end"], fold["val_end"]
            X_t, y_t = X_train[:t_end], y_train[:t_end]
            X_v, y_v = X_train[t_end:v_end], y_train[t_end:v_end]
            if len(X_t) == 0 or len(X_v) == 0:
                continue
            m = XGBRegressor(**params, random_state=42, n_jobs=-1, verbosity=0)
            m.fit(X_t, y_t)
            preds = np.maximum(0.0, m.predict(X_v))
            fold_rmses.append(mean_squared_error(y_v, preds) ** 0.5)
        return float(np.mean(fold_rmses)) if fold_rmses else float("inf")

    study = optuna.create_study(direction="minimize")
    study.optimize(objective, n_trials=n_trials, show_progress_bar=False)
    return study.best_params, study.best_value

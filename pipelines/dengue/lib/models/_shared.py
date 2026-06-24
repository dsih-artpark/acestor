"""Shared utilities used by multiple model implementations."""

from __future__ import annotations

import logging

import pandas as pd
from sklearn.preprocessing import OneHotEncoder

log = logging.getLogger(__name__)


def _lag(
    df: pd.DataFrame,
    spatial_col: str,
    lag_temp: list[int],
    lag_rainfall: list[int],
    lag_humidity: list[int],
    lag_cases: list[int] | None = None,
) -> pd.DataFrame:
    for lg in lag_temp:
        df[f"temp_lag_{lg}"] = df.groupby(spatial_col)["t2m_mean"].shift(lg)
    for lg in lag_rainfall:
        df[f"rainfall_lag_{lg}"] = df.groupby(spatial_col)["tp_sum"].shift(lg)
    for lg in lag_humidity:
        df[f"relative_humidity_lag_{lg}"] = df.groupby(spatial_col)["d2m_mean"].shift(
            lg
        )
    for lg in lag_cases or []:
        df[f"case_lag_{lg}"] = df.groupby(spatial_col)["case"].shift(lg)
    return df


def _one_hot(df: pd.DataFrame, cols: list[str] | None = None) -> pd.DataFrame:
    if cols is None:
        cols = ["ISOWeek"]
    enc = OneHotEncoder(sparse_output=False)
    encoded = enc.fit_transform(df[cols])
    return pd.DataFrame(encoded, columns=enc.get_feature_names_out(cols))


def recursive_forecast(
    model,
    df0: pd.DataFrame,
    *,
    spatial_col: str,
    feature_cols: list[str],
    lag_cases: list[int],
    future_dates: list[pd.Timestamp],
    clip_multiplier: float | None = None,
    train_max: float | None = None,
    debug: bool = False,
    freeze_weather_at_origin: bool = False,
) -> tuple[pd.DataFrame, list[dict]]:
    """Roll a one-step model forward over multiple weeks (recursive multi-step).

    For each future week, the non-case features (weather/iso lags) are taken
    frozen from that week's precomputed row in ``df0`` — they're known ahead of
    time. The ``case_lag_*`` features are filled hybrid-Y: a lag pointing at an
    already-forecast future week uses that prediction, one pointing at an observed
    week uses the observed value, and one before the series start is zero-padded.
    Each week's prediction is written back so later weeks' case lags see it.

    Predictions are floored at 0. If ``clip_multiplier`` is set they are also
    capped at ``clip_multiplier * train_max`` (off by default — parity with the
    reference; a cap firing is recorded in ``was_clipped``).

    A region is skipped entirely if any of its frozen (non-case) features are NaN
    for a future week (no weather coverage), mirroring the prior valid-mask drop.

    Returns ``(predictions_df, debug_records)``. predictions_df columns:
    ``spatial_col``, ``recordDate``, ``prediction``, ``was_clipped``. debug_records
    (only when ``debug``) carry per-lag value+source and raw/clipped predictions.
    """
    case_lag_cols = {
        lg: f"case_lag_{lg}" for lg in lag_cases if f"case_lag_{lg}" in feature_cols
    }
    non_case_cols = [c for c in feature_cols if c not in case_lag_cols.values()]
    upper = (
        clip_multiplier * train_max
        if clip_multiplier and train_max is not None
        else None
    )
    future = sorted(pd.Timestamp(d) for d in future_dates)
    future_set = set(future)

    df0 = df0.copy()
    df0["recordDate"] = pd.to_datetime(df0["recordDate"])

    out_rows: list[dict] = []
    debug_rows: list[dict] = []

    for rid, g in df0.groupby(spatial_col):
        g = g.sort_values("recordDate")
        case_by_date = {
            d: float(c) if pd.notna(c) else None
            for d, c in zip(g["recordDate"], g["case"])
        }
        observed_dates = [d for d in g["recordDate"] if d not in future_set]
        if not observed_dates:
            continue
        origin = max(observed_dates)
        first_date = g["recordDate"].min()
        rowmap = {r["recordDate"]: r for r in g.to_dict("records")}

        # Persistence: freeze weather/exogenous lags at the forecast origin's row
        # for every forecast week. Otherwise each future week advances to its own
        # precomputed row (requires lag >= horizon so the referenced week is
        # observed).
        if freeze_weather_at_origin:
            if origin not in rowmap or any(
                pd.isna(rowmap[origin][c]) for c in non_case_cols
            ):
                log.debug(
                    "recursive_forecast: skipping region %s — NaN non-case "
                    "features at origin",
                    rid,
                )
                continue
        elif any(
            d not in rowmap or any(pd.isna(rowmap[d][c]) for c in non_case_cols)
            for d in future
        ):
            # Skip region if any frozen feature is missing for a future week.
            log.debug(
                "recursive_forecast: skipping region %s — NaN non-case features", rid
            )
            continue

        for d in future:
            src_row = rowmap[origin] if freeze_weather_at_origin else rowmap[d]
            x = {c: src_row[c] for c in non_case_cols}
            lag_trace: dict = {}
            for lg, col in case_lag_cols.items():
                zdate = d - pd.Timedelta(weeks=lg)
                if zdate < first_date:
                    val, src = 0.0, "zero_pad"
                else:
                    v = case_by_date.get(zdate)
                    val = v if v is not None else 0.0
                    src = "predicted" if zdate > origin else "observed"
                x[col] = val
                lag_trace[col] = (val, src)

            raw = float(model.predict([[x[c] for c in feature_cols]])[0])
            pred = max(0.0, raw)
            was_clipped = False
            if upper is not None and pred > upper:
                pred, was_clipped = upper, True
            case_by_date[d] = pred

            out_rows.append(
                {
                    spatial_col: rid,
                    "recordDate": d,
                    "prediction": pred,
                    "was_clipped": was_clipped,
                }
            )
            if debug:
                drow = {
                    spatial_col: rid,
                    "recordDate": d,
                    "raw_prediction": raw,
                    "prediction": pred,
                    "was_clipped": was_clipped,
                }
                for col, (val, src) in lag_trace.items():
                    drow[f"{col}_value"] = val
                    drow[f"{col}_source"] = src
                debug_rows.append(drow)

    cols = [spatial_col, "recordDate", "prediction", "was_clipped"]
    return pd.DataFrame(out_rows, columns=cols), debug_rows

"""Ensemble strategies for combining per-model predictions.

Registry usage
--------------
New strategies need:
1. A class implementing the ``BaseEnsemble`` Protocol (``combine``).
2. A ``@register_ensemble("name")`` decorator.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

import pandas as pd

log = logging.getLogger(__name__)


@runtime_checkable
class BaseEnsemble(Protocol):
    def combine(self, dfs: list[pd.DataFrame], *, spatial_col: str) -> pd.DataFrame: ...


_ENSEMBLE_REGISTRY: dict[str, BaseEnsemble] = {}


def register_ensemble(name: str):
    def decorator(cls: type) -> type:
        _ENSEMBLE_REGISTRY[name] = cls()
        return cls

    return decorator


def get_ensemble(name: str) -> BaseEnsemble:
    if name not in _ENSEMBLE_REGISTRY:
        raise KeyError(
            f"Unknown ensemble '{name}'. Available: {sorted(_ENSEMBLE_REGISTRY)}"
        )
    return _ENSEMBLE_REGISTRY[name]


@register_ensemble("mean")
class MeanEnsemble:
    """Arithmetic mean of predictions per (region, date, ...) group.

    Preserves the behaviour of ``predictions.ensemble_predictions`` so the
    default config remains byte-identical to the pre-registry pipeline.
    """

    def combine(self, dfs: list[pd.DataFrame], *, spatial_col: str) -> pd.DataFrame:
        combined = pd.concat(dfs, ignore_index=True)
        rows_before = len(combined)
        # Exclude ISOWeek from grouping — each model computes it from its own
        # training tail (RF/XGB use `last_4` recordDates; TSE uses a 14-day-
        # earlier cutoff), so per-model ISOWeek values diverge for the same
        # target date. Grouping by it would split what should be one ensemble
        # row per (region, date, ...) into two, breaking downstream sanity
        # checks (issue #101). We recompute ISOWeek below from recordDate so
        # the output reflects the target week's true ISO week.
        group_cols = [
            c for c in combined.columns if c not in ("prediction", "model", "ISOWeek")
        ]

        nan_key_cols = [c for c in group_cols if combined[c].isna().any()]
        if nan_key_cols:
            for col in nan_key_cols:
                affected = (
                    combined.loc[combined[col].isna(), spatial_col].unique().tolist()
                )
                log.warning(
                    "MeanEnsemble: column '%s' has NaN values for %d region(s) %s "
                    "— pandas groupby will silently DROP these rows",
                    col,
                    len(affected),
                    affected,
                )

        ensembled = combined.groupby(group_cols)["prediction"].mean().reset_index()
        rows_after = len(ensembled)
        if rows_before != rows_after:
            log.warning(
                "MeanEnsemble: %d row(s) dropped by groupby (%d → %d) due to "
                "NaN keys in columns: %s",
                rows_before - rows_after,
                rows_before,
                rows_after,
                nan_key_cols,
            )

        if "recordDate" in ensembled.columns:
            # .astype(int) — match the int64 dtype used elsewhere in the pipeline
            # (train_and_predict.py:142), rather than the UInt32 that
            # dt.isocalendar().week returns.
            ensembled["ISOWeek"] = (
                pd.to_datetime(ensembled["recordDate"])
                .dt.isocalendar()
                .week.astype(int)
            )

        ensembled["model"] = "ensembleModel"
        return ensembled

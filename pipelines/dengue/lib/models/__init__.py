"""Prediction models for dengue forecasting.

Registry usage
--------------
New models only need two things:
1. A class implementing the ``BaseModel`` Protocol (``predict`` + ``threshold_to_date``).
2. A ``@register("name")`` decorator and an import in this file's footer.

Then set ``model.models: [name]`` (or add to the list) in any pipeline YAML.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import pandas as pd

if TYPE_CHECKING:
    from pipelines.dengue.configs import (
        TrainPredictConfig as TrainPredictConfig,
    )


@dataclass
class ModelContext:
    """Everything a model might need; models pull what they need."""

    merged_df: pd.DataFrame
    case_df: pd.DataFrame
    cfg: Any  # TrainPredictConfig — Any avoids circular import at runtime
    pred_upto: pd.Timestamp
    cutoff_case: pd.Timestamp


@runtime_checkable
class BaseModel(Protocol):
    def predict(self, ctx: ModelContext) -> pd.DataFrame: ...
    def threshold_to_date(self, ctx: ModelContext) -> pd.Timestamp: ...


_REGISTRY: dict[str, BaseModel] = {}


def register(name: str):
    """Class decorator that instantiates the class and stores it in _REGISTRY."""

    def decorator(cls: type) -> type:
        _REGISTRY[name] = cls()
        return cls

    return decorator


def get_model(name: str) -> BaseModel:
    if name not in _REGISTRY:
        raise KeyError(f"Unknown model '{name}'. Available: {sorted(_REGISTRY)}")
    return _REGISTRY[name]


# Import model modules AFTER registry is defined so @register decorators can run.
from pipelines.dengue.lib.models import nbr as _nbr_mod  # noqa: F401, E402
from pipelines.dengue.lib.models import tse as _tse_mod  # noqa: F401, E402
from pipelines.dengue.lib.models import rf as _rf_mod  # noqa: F401, E402

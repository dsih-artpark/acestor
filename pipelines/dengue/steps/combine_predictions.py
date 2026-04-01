from __future__ import annotations

import io
from dataclasses import dataclass
from typing import ClassVar

import pandas as pd

from acestor import BaseStep, PipelineContext
from pipelines.dengue.lib import predictions as pred_lib
from pipelines.dengue.results import (
    CombinedPredictionsResult,
    CutoffDatesResult,
    PredictionResult,
)


@dataclass(frozen=True)
class CombinePredictionsInputs:
    train_and_predict: PredictionResult
    identify_cutoff_dates: CutoffDatesResult


class CombinePredictionsStep(
    BaseStep[CombinePredictionsInputs, CombinedPredictionsResult]
):
    input_type: ClassVar[type] = CombinePredictionsInputs

    def run(
        self, context: PipelineContext, inputs: CombinePredictionsInputs
    ) -> CombinedPredictionsResult:
        pred_csv = context.artifacts.read_text(
            inputs.train_and_predict.predictions_csv_path
        )
        df = pd.read_csv(io.StringIO(pred_csv))

        combined = pred_lib.combine_all_predictions(
            [df],
            inputs.identify_cutoff_dates.prediction_dates,
        )

        end_str = pd.Timestamp.today().date().strftime("%Y%m%d")
        month_str = inputs.train_and_predict.month_string
        dest = context.artifact_path(f"results/Predictions_{month_str}_{end_str}.csv")
        context.artifacts.write_text(combined.to_csv(index=False), dest)
        context.log.info("combine_predictions: %d rows", len(combined))

        return CombinedPredictionsResult(combined_csv_path=dest)

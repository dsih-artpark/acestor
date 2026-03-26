from __future__ import annotations

from typing import ClassVar

import pandas as pd

from acestor import BaseStep, NoInputs, PipelineContext
from pipelines.gba_dengue.lib import sampling
from pipelines.gba_dengue.results import SamplingDayResult


class IdentifySamplingDayStep(BaseStep[NoInputs, SamplingDayResult]):
    input_type: ClassVar[type] = NoInputs

    def run(self, context: PipelineContext, inputs: NoInputs) -> SamplingDayResult:
        raw_run_date = (context.config or {}).get("run", {}).get("run_date", "")
        run_date = (
            pd.Timestamp(str(raw_run_date)).normalize()
            if raw_run_date
            else pd.Timestamp.today().normalize()
        )
        day = sampling.get_day_abbreviation(run_date)

        context.write_artifact_json(
            "sampling_day.json",
            {
                "run_date": str(run_date.date()),
                "sampling_day": day,
            },
        )
        context.log.info("sampling_day=%s run_date=%s", day, run_date.date())

        return SamplingDayResult(run_date=str(run_date.date()), sampling_day=day)

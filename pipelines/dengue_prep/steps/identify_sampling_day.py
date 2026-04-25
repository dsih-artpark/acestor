from __future__ import annotations

from typing import ClassVar

import pandas as pd

from acestor import BaseStep, NoInputs, PipelineContext
from pipelines.dengue.lib import sampling
from pipelines.dengue_prep.results import PrepSamplingDayResult


class PrepIdentifySamplingDayStep(BaseStep[NoInputs, PrepSamplingDayResult]):
    input_type: ClassVar[type] = NoInputs

    def run(self, context: PipelineContext, inputs: NoInputs) -> PrepSamplingDayResult:
        raw_run_date = (context.config or {}).get("run", {}).get("run_date", "")
        if not raw_run_date:
            run_date = pd.Timestamp.today().normalize()
            context.log.warning(
                "identify_sampling_day: run_date not set in config — defaulting to today (%s); "
                "set run.run_date in your config for reproducible runs",
                run_date.date(),
            )
        else:
            run_date = pd.Timestamp(str(raw_run_date)).normalize()

        day = sampling.get_day_abbreviation(run_date)

        context.write_artifact_json(
            "sampling_day.json",
            {"run_date": str(run_date.date()), "sampling_day": day},
        )
        context.log.info(
            "identify_sampling_day: run_date=%s weekday=%s → sampling_day=%s "
            "(controls which weekday is used as the weekly data cutoff in downstream steps)",
            run_date.date(),
            run_date.day_name(),
            day,
        )

        return PrepSamplingDayResult(run_date=str(run_date.date()), sampling_day=day)

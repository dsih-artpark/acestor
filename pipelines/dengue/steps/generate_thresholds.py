from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any, ClassVar

import pandas as pd

from acestor import BaseStep, PipelineContext
from pipelines.dengue.configs import (
    ThresholdsConfig,
    resolve_threshold_config,
    _section,
)
from pipelines.dengue.lib import thresholds
from pipelines.dengue.lib.thresholds import (
    ThresholdContext,
    combine_thresholds,
    get_threshold_method,
)
from pipelines.dengue.results import CutoffDatesResult, ThresholdsResult


@dataclass(frozen=True)
class GenerateThresholdsInputs:
    identify_cutoff_dates: CutoffDatesResult


class GenerateThresholdsStep(BaseStep[GenerateThresholdsInputs, ThresholdsResult]):
    input_type: ClassVar[type] = GenerateThresholdsInputs

    def run(
        self, context: PipelineContext, inputs: GenerateThresholdsInputs
    ) -> ThresholdsResult:
        cfg = ThresholdsConfig.from_raw(_section(context.config, "thresholds"))
        raw_threshold_configs: dict[str, Any] = dict(
            context.config.get("threshold_configs") or {}
        )
        unknown = set(raw_threshold_configs) - set(cfg.methods)
        if unknown:
            raise ValueError(
                f"threshold_configs contains keys not in thresholds.methods: {sorted(unknown)}. "
                f"thresholds.methods = {cfg.methods}"
            )

        case_path = context.artifact_path(
            f"datasets/cases_{cfg.region_type}_sampled.csv"
        )
        case_csv = context.artifacts.read_text(case_path)
        df = pd.read_csv(io.StringIO(case_csv))

        if "region_id" not in df.columns:
            for candidate in [
                "location.admin4.ID",
                "location.admin3.ID",
                "location.admin2.ID",
            ]:
                if candidate in df.columns:
                    df.rename(columns={candidate: "region_id"}, inplace=True)
                    break
        if "date" not in df.columns and "metadata.primaryDate" in df.columns:
            df.rename(columns={"metadata.primaryDate": "date"}, inplace=True)

        aligned = thresholds.align_dates_all_regions(df)

        method_dfs = []
        for method_name in cfg.methods:
            method_cfg = resolve_threshold_config(
                cfg, raw_threshold_configs.get(method_name, {})
            )
            ctx = ThresholdContext(
                n_weeks=method_cfg.n_weeks,
                historical_n_years=method_cfg.historical_n_years,
                excluded_years=method_cfg.excluded_years,
                included_years=method_cfg.included_years,
            )
            fn = get_threshold_method(method_name)
            method_dfs.append(fn(aligned, ctx))

        combined = combine_thresholds(method_dfs)

        dest = context.artifact_path(
            f"datasets/thresholds/{cfg.region_type}_all_thresholds.csv"
        )
        context.artifacts.write_text(combined.to_csv(index=False), dest)
        context.log.info(
            "generate_thresholds: %d rows for %s", len(combined), cfg.region_type
        )

        return ThresholdsResult(thresholds_csv_path=dest, region_type=cfg.region_type)

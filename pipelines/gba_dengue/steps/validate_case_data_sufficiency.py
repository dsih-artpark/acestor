"""Gate: ensure sampled case data is sufficient before cutoffs / modeling."""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import ClassVar

import pandas as pd

from acestor import BaseStep, PipelineContext
from pipelines.gba_dengue.configs import CaseSufficiencyConfig, _section
from pipelines.gba_dengue.lib import case_data
from pipelines.gba_dengue.results import DataSufficiencyResult, ParseCaseDataResult


@dataclass(frozen=True)
class ValidateCaseDataSufficiencyInputs:
    parse_nonstd_case_data: ParseCaseDataResult


class ValidateCaseDataSufficiencyStep(
    BaseStep[ValidateCaseDataSufficiencyInputs, DataSufficiencyResult]
):
    input_type: ClassVar[type] = ValidateCaseDataSufficiencyInputs

    def run(
        self,
        context: PipelineContext,
        inputs: ValidateCaseDataSufficiencyInputs,
    ) -> DataSufficiencyResult:
        cfg = CaseSufficiencyConfig.from_raw(
            _section(context.config, "data.case_sufficiency")
        )
        case_res = inputs.parse_nonstd_case_data

        if not cfg.enabled:
            context.log.info("validate_case_data_sufficiency: disabled by config")
            return DataSufficiencyResult(case_result=case_res)

        region_col = cfg.region_column or case_data._REGION_ADMIN_COL.get(
            case_res.region_type, "region_id"
        )
        date_col = cfg.date_column or "metadata.primaryDate"

        csv_text = context.artifacts.read_text(case_res.sampled_csv_path)
        df = pd.read_csv(io.StringIO(csv_text), low_memory=False)

        missing = [
            c for c in (cfg.case_column, region_col, date_col) if c not in df.columns
        ]
        if missing:
            raise ValueError(
                "validate_case_data_sufficiency: sampled case CSV missing columns "
                f"{missing}. Parsed region_type={case_res.region_type!r}. "
                "Set data.case_sufficiency.region_column / date_column if needed."
            )

        n = len(df)
        n_regions = df[region_col].nunique()
        dts = pd.to_datetime(df[date_col], errors="coerce")
        if dts.isna().all():
            raise ValueError(
                "validate_case_data_sufficiency: no valid dates in column "
                f"{date_col!r}; cannot compute span."
            )
        span_days = (dts.max() - dts.min()).days

        reasons: list[str] = []
        if n < cfg.min_total_rows:
            reasons.append(f"row_count {n} < min_total_rows {cfg.min_total_rows}")
        if n_regions < cfg.min_distinct_regions:
            reasons.append(
                f"distinct_regions {n_regions} < min_distinct_regions {cfg.min_distinct_regions}"
            )
        if span_days < cfg.min_date_span_days:
            reasons.append(
                f"date_span_days {span_days} < min_date_span_days {cfg.min_date_span_days}"
            )

        if reasons:
            msg = (
                "Insufficient case data for downstream modeling (see data.case_sufficiency): "
                + "; ".join(reasons)
            )
            context.log.error("validate_case_data_sufficiency: %s", msg)
            raise ValueError(msg)

        context.log.info(
            "validate_case_data_sufficiency: ok rows=%d regions=%d span_days=%d",
            n,
            n_regions,
            span_days,
        )
        return DataSufficiencyResult(case_result=case_res)

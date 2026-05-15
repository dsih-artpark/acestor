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


def _apply_rolling_fallback(
    historical_df: pd.DataFrame,
    rolling_df: pd.DataFrame,
    spatial_col: str,
) -> pd.DataFrame:
    """Replace historical Mean=0 rows with prev_nweeks values (PRISM-H §4.4).

    When the historical method produces Mean=0 for a (region, date) pair,
    substitute the prev_nweeks Mean and StdDev for that same pair — but only
    when the prev_nweeks Mean is present and > 0. All other columns are preserved.
    """
    out = historical_df.copy()
    zero_mask = out["Mean"] == 0
    if not zero_mask.any():
        return out

    # Index rolling_df by (spatial_col, date) for fast O(1) lookup
    rolling_indexed = rolling_df.set_index([spatial_col, "date"])
    for idx in out[zero_mask].index:
        key = (out.at[idx, spatial_col], out.at[idx, "date"])
        if key in rolling_indexed.index:
            rolling_row = rolling_indexed.loc[key]
            # .loc can return a Series (single match) or DataFrame (multiple matches)
            if isinstance(rolling_row, pd.DataFrame):
                rolling_mean = rolling_row["Mean"].iloc[0]
                rolling_std = rolling_row["StdDev"].iloc[0]
            else:
                rolling_mean = rolling_row["Mean"]
                rolling_std = rolling_row["StdDev"]
            if pd.notna(rolling_mean) and rolling_mean > 0:
                out.at[idx, "Mean"] = rolling_mean
                out.at[idx, "StdDev"] = rolling_std
    return out


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
            _section(context.config, "thresholds").get("method_configs") or {}
        )
        unknown = set(raw_threshold_configs) - set(cfg.methods)
        if unknown:
            raise ValueError(
                f"threshold_configs contains keys not in thresholds.methods: {sorted(unknown)}. "
                f"thresholds.methods = {cfg.methods}"
            )

        case_path = context.artifact_path(f"inputs/cases_{cfg.region_type}_sampled.csv")
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
        method_df_by_name: dict[str, pd.DataFrame] = {}
        for method_name in cfg.methods:
            method_cfg = resolve_threshold_config(
                cfg, raw_threshold_configs.get(method_name, {})
            )
            ctx = ThresholdContext(
                n_weeks=method_cfg.n_weeks,
                historical_n_years=method_cfg.historical_n_years,
                excluded_years=method_cfg.excluded_years,
                included_years=method_cfg.included_years,
                recent_weeks=method_cfg.recent_weeks,
                sd_window_weeks=method_cfg.sd_window_weeks,
                weight_recent=method_cfg.weight_recent,
                weight_seasonal=method_cfg.weight_seasonal,
            )
            fn = get_threshold_method(method_name)
            result_df = fn(aligned, ctx)
            method_dfs.append(result_df)
            method_df_by_name[method_name] = result_df

        # PRISM-H §4.4 μ=0 fallback: replace historical Mean=0 with prev_nweeks values
        if "historical" in method_df_by_name and "prev_nweeks" in method_df_by_name:
            patched = _apply_rolling_fallback(
                method_df_by_name["historical"],
                method_df_by_name["prev_nweeks"],
                spatial_col="region_id",
            )
            # Replace the historical entry in method_dfs in-place
            hist_list_idx = cfg.methods.index("historical")
            method_dfs[hist_list_idx] = patched

        combined = combine_thresholds(method_dfs)

        dest = context.artifact_path(
            f"inputs/thresholds/{cfg.region_type}_all_thresholds.csv"
        )
        context.artifacts.write_text(combined.to_csv(index=False), dest)
        context.log.info(
            "generate_thresholds: %d rows for %s", len(combined), cfg.region_type
        )

        return ThresholdsResult(thresholds_csv_path=dest, region_type=cfg.region_type)

"""Typed result dataclasses for every step in the dengue pipeline.

Each frozen dataclass is the *contract* between a step and its downstream
consumers.  All step output types live in this single file so that the full
data-flow shape of the pipeline is visible at a glance.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SamplingDayResult:
    run_date: str
    sampling_day: str


@dataclass(frozen=True)
class CaseDownloadResult:
    enabled: bool
    copied_files: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class WeatherDownloadResult:
    enabled: bool
    downloaded_files: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DataSufficiencyResult:
    """Output of ``validate_case_data_sufficiency``; carries case parse through the gate."""

    case_result: ParseCaseDataResult


@dataclass(frozen=True)
class ParseCaseDataResult:
    """Case parse outputs.

    ``sampled_csv_path`` / ``region_type`` follow the **last** entry in ``data.case_parse.region_types``
    (SOT-style order: list ``corp`` then ``zone`` so zone feeds cutoffs). See ``sampled_by_region_type`` for all.
    """

    sampled_csv_path: str
    region_type: str
    sampled_by_region_type: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ParseWeatherDataResult:
    weather_csv_path: str
    region_type: str


@dataclass(frozen=True)
class CutoffDatesResult:
    cutoff: str
    pred_upto: str
    cutoff_case: str
    cutoff_weather: str
    sampling_day: str
    run_date: str
    prediction_dates: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ThresholdsResult:
    thresholds_csv_path: str
    region_type: str


@dataclass(frozen=True)
class PredictionResult:
    predictions_csv_path: str
    region_type: str
    month_string: str


@dataclass(frozen=True)
class CombinedPredictionsResult:
    combined_csv_path: str


@dataclass(frozen=True)
class ThresholdAssessmentResult:
    best_method_by_region: dict[str, str]  # {region_type: csv_path}


@dataclass(frozen=True)
class MapsResult:
    map_paths: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ReportResult:
    """``report_path`` is the ``rep_dict`` JSON storage key.

    ``tex_path`` / ``latex_bundle_zip_path`` are set when those artifacts are emitted.
    ``pdf_path`` / ``maps_zip_path`` are filesystem paths set when those files are produced.
    """

    report_path: str
    pdf_path: str | None = None
    maps_zip_path: str | None = None
    tex_path: str | None = None
    latex_bundle_zip_path: str | None = None


@dataclass(frozen=True)
class SendReportResult:
    """Outcome of the ``send_report`` email step."""

    sent: bool
    reason: str  # e.g. "disabled", "not_subscribed", "sent", "smtp_incomplete", "send_failed"


@dataclass(frozen=True)
class NotifyRunResult:
    """Outcome of the optional SMTP notification step (success-path only)."""

    notified: bool
    reason: str  # e.g. "disabled", "not_subscribed", "sent", "smtp_incomplete", "send_failed"

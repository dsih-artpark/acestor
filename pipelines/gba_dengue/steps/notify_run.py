"""Send run success notification email when ``email:`` is enabled (DAG terminal step)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import ClassVar

from acestor import BaseStep, PipelineContext
from acestor.infra.run_notification import send_run_notification_email_if_configured
from pipelines.gba_dengue.results import NotifyRunResult, ReportResult


@dataclass(frozen=True)
class NotifyRunInputs:
    generate_report: ReportResult


class NotifyRunStep(BaseStep[NotifyRunInputs, NotifyRunResult]):
    """SMTP notification after a successful ``generate_report`` (see top-level ``email:`` config)."""

    input_type: ClassVar[type] = NotifyRunInputs

    def run(self, context: PipelineContext, inputs: NotifyRunInputs) -> NotifyRunResult:
        _ = inputs
        cfg = context.config if isinstance(context.config, dict) else {}
        email_cfg = cfg.get("email") if isinstance(cfg.get("email"), dict) else {}
        if not email_cfg.get("enabled"):
            return NotifyRunResult(notified=False, reason="disabled")

        on = email_cfg.get("on") or []
        if "success" not in on:
            return NotifyRunResult(notified=False, reason="not_subscribed")

        end_ts = datetime.now(timezone.utc).isoformat()
        start_ts = context.run_started_at or end_ts
        step_names = sorted(context.completed_steps)

        sent = send_run_notification_email_if_configured(
            config=cfg,
            run_id=context.run_id,
            status="success",
            start_ts=start_ts,
            end_ts=end_ts,
            step_names=step_names,
            failure_detail="",
            logger=context.log,
        )

        if sent:
            context.log.info("notify_run: success notification sent")
            return NotifyRunResult(notified=True, reason="sent")

        # enabled + subscribed but helper did not send (incomplete SMTP, etc.)
        return NotifyRunResult(notified=False, reason="smtp_incomplete")

"""Run-level email notifications driven by top-level ``email:`` in pipeline config."""

from __future__ import annotations

import logging
from typing import Any, Mapping

from acestor.infra.email import send_email


def send_run_notification_email_if_configured(
    *,
    config: Mapping[str, Any],
    run_id: str,
    status: str,
    start_ts: str,
    end_ts: str,
    step_names: list[str],
    failure_detail: str = "",
    logger: logging.Logger | None = None,
) -> bool:
    """Send SMTP notification if ``email.enabled`` and ``status`` is listed in ``email.on``.

    Returns True if an email was sent, False otherwise (disabled, not subscribed, or
    missing SMTP settings).
    """
    email_cfg = config.get("email") if isinstance(config, dict) else None
    if not isinstance(email_cfg, dict) or not email_cfg.get("enabled"):
        return False

    on = email_cfg.get("on") or []
    if status not in on:
        return False

    smtp_cfg = email_cfg.get("smtp") or {}
    host = smtp_cfg.get("host")
    port = int(smtp_cfg.get("port", 587))
    username = smtp_cfg.get("username")
    password = smtp_cfg.get("password")
    use_tls = bool(smtp_cfg.get("use_tls", True))
    sender = email_cfg.get("from")
    recipients = email_cfg.get("to") or []

    if not host or not sender or not recipients:
        if logger is not None:
            logger.warning(
                "email.enabled but missing host/from/to; skipping notification for run %s",
                run_id,
            )
        return False

    pipeline_name = (
        (config.get("pipeline") or {}).get("name") if isinstance(config, dict) else None
    )
    pipeline_name = pipeline_name or "pipeline"
    subject = f"[acestor] {pipeline_name} run {status} (run_id={run_id})"
    body = (
        f"Pipeline: {pipeline_name}\n"
        f"Run ID: {run_id}\n"
        f"Status: {status}\n"
        f"Started: {start_ts}\n"
        f"Finished: {end_ts}\n"
        f"Steps: {', '.join(step_names)}\n"
    )
    if failure_detail:
        body += f"\nFailure:\n{failure_detail}\n"

    try:
        send_email(
            host=host,
            port=port,
            username=username,
            password=password,
            use_tls=use_tls,
            sender=sender,
            recipients=recipients,
            subject=subject,
            body=body,
        )
    except Exception:
        if logger is not None:
            logger.exception("Failed to send notification email for run %s", run_id)
        return False

    return True

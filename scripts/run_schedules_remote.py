"""Remote-run scheduler — a sibling of ``run_schedules.py`` that shells out
to ``acestor.remote`` (spawn EC2, run pipeline, terminate) instead of
``acestor.run`` (local process).

Meant to live on the caller box (see docs/deployment/remote-runner.md).
Edit ``PIPELINES`` below, then:

    Foreground:   python scripts/run_schedules_remote.py
    Background:   nohup python scripts/run_schedules_remote.py > .acestor/remote-scheduler.out 2>&1 &

Cross-job dependency chaining is honoured via the ``depends_on`` field:
a job with ``depends_on: "ka-prep"`` won't run unless ``ka-prep`` completed
successfully in the same UTC calendar day. The dependency state lives
in-memory — a scheduler restart forgets prior successes, so any downstream
job that fires before its dependency has run again that day gets skipped
(a warning is logged, not an error — trigger it manually if you care).

Dashboard credentials are read from ``~/.env`` via python-dotenv. Put:

    DASHBOARD_URL=https://apps.artpark.ai/disease-dashboard-staging
    DASHBOARD_CLIENT_ID=admin@dengue.local
    DASHBOARD_CLIENT_SECRET=...

there and never hardcode them in this file.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = ROOT / "logs"

# Load ~/.env (dashboard creds etc.) before we read the environment.
load_dotenv(Path.home() / ".env")

# ── Static remote runner config ────────────────────────────────────────────────
AWS_KEY_NAME = os.environ.get("ACESTOR_AWS_KEY_NAME", "acestor-smoke-202607311430")
AWS_KEY_PATH = os.path.expanduser(
    os.environ.get("ACESTOR_AWS_KEY_PATH", "~/.ssh/acestor-smoke.pem")
)
# Tier-2 AMI baked 2026-08-03 (ubuntu 24.04 + python + uv + git/rsync/build-deps).
# Overridable via env so bumping to a new AMI doesn't need a code change.
AWS_AMI = os.environ.get("ACESTOR_AWS_AMI", "ami-0b8a9646e50319028")
FORWARD_ENV = (
    "DASHBOARD_URL",
    "DASHBOARD_CLIENT_ID",
    "DASHBOARD_CLIENT_SECRET",
    "DASHBOARD_SELECTED_REGION_ID",
)

# ── Configure your pipelines here ──────────────────────────────────────────────
# Each entry:
#   name             — short label used for job id + log path
#   cron             — 5-field crontab expression, UTC
#   pipeline         — module:callable, forwarded verbatim to acestor.remote
#   config           — path to yaml, forwarded verbatim
#   remote_instance  — EC2 shape (e.g. t3.large for prep, c7i.4xlarge for
#                      heavy forecast)
#   remote_lifecycle — "on-demand" (safer) or "spot" (cheaper, may be reclaimed)
#   depends_on       — optional job name; this one only runs if that one
#                      completed OK today (UTC calendar day)
PIPELINES = [
    {
        "name": "ka-prep",
        "cron": "0 2 * * *",  # 02:00 UTC daily
        "pipeline": "pipelines.dengue_prep.pipeline:build_pipeline",
        "config": "configs/ka_district_prep.yaml",
        "remote_instance": "t3.large",
        "remote_lifecycle": "on-demand",
    },
    {
        "name": "ka-forecast",
        "cron": "0 3 * * *",  # 03:00 UTC daily, after ka-prep
        "pipeline": "pipelines.dengue.pipeline:build_pipeline",
        "config": "configs/ka_district.yaml",
        "remote_instance": "c7i.4xlarge",
        "remote_lifecycle": "on-demand",
        "depends_on": "ka-prep",
    },
]
# ──────────────────────────────────────────────────────────────────────────────

# job_name → UTC date of last successful run. Used for depends_on checks.
# In-memory only; wipes on scheduler restart. That's fine — a missed
# dependency check just skips a run, doesn't produce wrong data.
LAST_SUCCESS: dict[str, date] = {}


def _print(msg: str) -> None:
    """Prefixed print so scheduler stdout mixes coherently with pipeline logs."""
    print(f"[{datetime.utcnow().isoformat()}Z] scheduler: {msg}", flush=True)


def _build_remote_cmd(spec: dict, run_id: str) -> list[str]:
    """Assemble the acestor.remote CLI invocation for ``spec``."""
    cmd = [
        sys.executable,
        "-m",
        "acestor.remote",
        "--pipeline",
        spec["pipeline"],
        "--config",
        spec["config"],
        "--run-id",
        run_id,
        "--remote-instance",
        spec["remote_instance"],
        "--remote-lifecycle",
        spec.get("remote_lifecycle", "on-demand"),
        "--aws-key-name",
        AWS_KEY_NAME,
        "--aws-key-path",
        AWS_KEY_PATH,
        "--aws-ami",
        AWS_AMI,
    ]
    for env_name in FORWARD_ENV:
        cmd.extend(["--forward-env", env_name])
    return cmd


def make_job(spec: dict):
    def run():
        name = spec["name"]

        dep = spec.get("depends_on")
        if dep:
            today = datetime.utcnow().date()
            if LAST_SUCCESS.get(dep) != today:
                _print(
                    f"SKIP {name}: dep {dep!r} has not completed OK today "
                    f"(last success: {LAST_SUCCESS.get(dep, 'never')})"
                )
                return

        now = datetime.utcnow()
        run_id = f"{name}_{now.strftime('%Y-%m-%d_%H%M%S')}"
        log_path = LOGS_DIR / name / now.strftime("%Y-%m-%d") / f"{run_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _print(f"START {name} → {log_path}")

        with open(log_path, "w") as f:
            result = subprocess.run(
                _build_remote_cmd(spec, run_id),
                stdout=f,
                stderr=f,
                cwd=str(ROOT),
            )

        if result.returncode == 0:
            LAST_SUCCESS[name] = datetime.utcnow().date()
            _print(f"OK    {name} — ran acestor.remote to completion")
        else:
            _print(f"FAIL  {name} — acestor.remote exited {result.returncode}")

    return run


def main():
    LOGS_DIR.mkdir(exist_ok=True)
    scheduler = BlockingScheduler(timezone="UTC")

    # Validate depends_on references before starting the loop.
    names = {p["name"] for p in PIPELINES}
    for p in PIPELINES:
        dep = p.get("depends_on")
        if dep and dep not in names:
            raise ValueError(
                f"pipeline {p['name']!r} depends_on {dep!r} which is not defined."
            )

    for p in PIPELINES:
        scheduler.add_job(
            make_job(p),
            CronTrigger.from_crontab(p["cron"], timezone="UTC"),
            id=p["name"],
            name=p["name"],
            misfire_grace_time=3600,
        )
        dep_note = f", depends_on={p['depends_on']}" if p.get("depends_on") else ""
        print(
            f"Scheduled: {p['name']}  ({p['cron']} UTC, "
            f"{p['remote_instance']} {p.get('remote_lifecycle', 'on-demand')}"
            f"{dep_note})"
        )

    print("Remote scheduler running. Ctrl+C to stop.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("Remote scheduler stopped.")


if __name__ == "__main__":
    main()

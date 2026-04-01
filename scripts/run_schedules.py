"""
Pipeline scheduler — edit PIPELINES below, then run:

    Foreground:   python scripts/run_schedules.py
    Background:   nohup python scripts/run_schedules.py > .acestor/scheduler.out 2>&1 &   (Linux/Mac)
                  start /B pythonw scripts/run_schedules.py                                (Windows)
"""

import subprocess
import sys
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = ROOT / "logs"

# ── Configure your pipelines here ─────────────────────────────────────────────
PIPELINES = [
    {
        "name": "gba-weekly",
        "cron": "*/1 * * * *",  # every 1m
        "pipeline": "pipelines.dengue.pipeline:build_pipeline",
        "config": "configs/gba_stage1_s3.yaml",
    },
    # {
    #     "name":     "another-pipeline",
    #     "cron":     "0 8 * * *",
    #     "pipeline": "pipelines.other:build_pipeline",
    #     "config":   "configs/other.yaml",
    # },
]
# ──────────────────────────────────────────────────────────────────────────────


def make_job(name: str, pipeline: str, config: str):
    def run():
        run_id = f"run-{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        log_path = LOGS_DIR / name / f"{run_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        print(
            f"[{datetime.now().isoformat()}] Starting {name} → {log_path}", flush=True
        )
        with open(log_path, "w") as f:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "acestor.run",
                    "--pipeline",
                    pipeline,
                    "--config",
                    config,
                    "--run-id",
                    run_id,
                ],
                stdout=f,
                stderr=f,
                cwd=str(ROOT),
            )
        print(
            f"[{datetime.now().isoformat()}] Finished {name} (exit={result.returncode})",
            flush=True,
        )

    return run


def main():
    scheduler = BlockingScheduler(timezone="UTC")

    for p in PIPELINES:
        scheduler.add_job(
            make_job(p["name"], p["pipeline"], p["config"]),
            CronTrigger.from_crontab(p["cron"]),
            id=p["name"],
            name=p["name"],
            misfire_grace_time=3600,  # retry if missed by up to 1 hour
        )
        print(f"Scheduled: {p['name']}  ({p['cron']})")

    print("Scheduler running. Press Ctrl+C to stop.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("Scheduler stopped.")


if __name__ == "__main__":
    main()

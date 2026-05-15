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
        "name": "ap-dengue-prep",
        "cron": "0 2 * * *",  # At 02:00, every day
        "pipeline": "pipelines.dengue_prep.pipeline:build_pipeline",
        "config": "configs/ap_district_prep.yaml",
    },
    {
        "name": "ap-dengue",
        "cron": "0 3 * * 0",  # At 03:00, only on Sunday (1 hour after prep)
        "pipeline": "pipelines.dengue.pipeline:build_pipeline",
        "config": "configs/ap_district_v3.yaml",
    },
    {
        "name": "ap-dengue-mandal",
        "cron": "0 4 * * 0",  # At 04:00, only on Sunday (1 hour after ap-dengue)
        "pipeline": "pipelines.dengue_downscale.pipeline:build_pipeline",
        "config": "configs/ap_district_to_mandal.yaml",
    },
]
# ──────────────────────────────────────────────────────────────────────────────


def make_job(name: str, pipeline: str, config: str):
    def run():
        now = datetime.now()
        run_id = f"{name}_{now.strftime('%Y-%m-%d_%H%M%S')}"
        log_path = LOGS_DIR / name / now.strftime("%Y-%m-%d") / f"{run_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        print(f"[{now.isoformat()}] Starting {name} → {log_path}", flush=True)
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
        status = (
            "OK" if result.returncode == 0 else f"FAILED (exit={result.returncode})"
        )
        print(f"[{datetime.now().isoformat()}] Finished {name} — {status}", flush=True)

    return run


def main():
    scheduler = BlockingScheduler(timezone="UTC")

    for p in PIPELINES:
        scheduler.add_job(
            make_job(p["name"], p["pipeline"], p["config"]),
            CronTrigger.from_crontab(p["cron"]),
            id=p["name"],
            name=p["name"],
            misfire_grace_time=3600,
        )
        print(f"Scheduled: {p['name']}  ({p['cron']})")

    print("Scheduler running. Press Ctrl+C to stop.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("Scheduler stopped.")


if __name__ == "__main__":
    main()

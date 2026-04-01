"""Install a crontab entry for the dengue pipeline from the YAML schedule config.

Usage:
    python scripts/install_schedule.py configs/gba_stage1_s3.yaml
"""

import subprocess
import sys
import yaml
from pathlib import Path

cfg = yaml.safe_load(Path(sys.argv[1]).read_text()).get("schedule", {})
cron = cfg.get("cron", "0 6 * * 1")
pipe = cfg.get("pipeline", "pipelines.dengue.pipeline:build_pipeline")
config = cfg.get("config", sys.argv[1])
root = Path(__file__).resolve().parent.parent
logs_dir = root / "logs"
logs_dir.mkdir(exist_ok=True)
uv = Path(
    subprocess.run(["which", "uv"], capture_output=True, text=True).stdout.strip()
    or "/usr/local/bin/uv"
)

line = (
    f"{cron}  RUN_ID=run-$(date +\\%Y\\%m\\%d_\\%H\\%M\\%S);"
    f" mkdir -p {logs_dir} &&"
    f" cd {root} && {uv} run python -m acestor.run"
    f" --pipeline {pipe} --config {config} --run-id $RUN_ID"
    f" >> {logs_dir}/$RUN_ID.log 2>&1\n"
)

subprocess.run(["crontab", "-"], input=line, text=True, check=True)
print(f"Installed: {line.strip()}")

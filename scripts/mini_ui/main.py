"""acestor mini-UI — read-only-ish operator dashboard for the caller.

Single-page FastAPI app. No database, no build step, no auth. Access is
IAM-controlled via SSM port-forwarding from the operator's laptop:

    aws ssm start-session --target <caller-instance-id> --region ap-south-1 \\
      --document-name AWS-StartPortForwardingSession \\
      --parameters '{"portNumber":["8000"],"localPortNumber":["8080"]}'
    # then browse http://localhost:8080

Everything the UI shows is read from files already on disk:
  * ~/.acestor/remote_runs.jsonl      — one line per remote run
  * ~/.acestor/scheduler_attempts.jsonl — per-attempt tracking
  * ~/acestor-work/artifacts/**       — actual pipeline outputs
  * ~/acestor-work/configs/*.yaml     — configs used
  * scripts/run_schedules_remote.py::STATES — DAGs + crons

Trigger endpoint spawns acestor.remote as a background subprocess.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

# ── Paths ─────────────────────────────────────────────────────────────────────
HOME = Path(os.environ.get("HOME", "/home/ubuntu"))
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LEDGER_PATH = HOME / ".acestor" / "remote_runs.jsonl"
ATTEMPTS_PATH = HOME / ".acestor" / "scheduler_attempts.jsonl"
ARTIFACTS_ROOT = REPO_ROOT / "artifacts"
CONFIGS_ROOT = REPO_ROOT / "configs"
LOGS_ROOT = REPO_ROOT / "logs"

# Import the scheduler's STATES dict so we can show the schedule without
# duplicating config. sys.path so the import works when running from the
# caller box where scripts/ is on the search path.
sys.path.insert(0, str(REPO_ROOT / "scripts"))
try:
    from run_schedules_remote import STATES  # type: ignore[import-not-found]
except Exception:  # pragma: no cover — imported at runtime
    STATES = {}

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

app = FastAPI(title="acestor mini-UI", docs_url=None, redoc_url=None)


# ── Helpers ───────────────────────────────────────────────────────────────────
def _read_jsonl(path: Path, limit: int | None = None) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if limit is not None:
        rows = rows[-limit:]
    return rows


def _next_fire_time(cron: str) -> str:
    try:
        trig = CronTrigger.from_crontab(cron, timezone=timezone.utc)
        nxt = trig.get_next_fire_time(None, datetime.now(timezone.utc))
        return nxt.isoformat() if nxt else "-"
    except Exception as exc:
        return f"err: {exc}"


def _running_compute() -> list[dict[str, Any]]:
    """Query AWS for compute EC2s tagged by acestor.remote."""
    region = os.environ.get("AWS_REGION", "ap-south-1")
    try:
        out = subprocess.check_output(
            [
                "aws",
                "ec2",
                "describe-instances",
                "--region",
                region,
                "--filters",
                "Name=tag:acestor:remote-runner,Values=true",
                "Name=instance-state-name,Values=running,pending",
                "--query",
                "Reservations[].Instances[].{id:InstanceId,type:InstanceType,"
                "launched:LaunchTime,lifecycle:Tags[?Key==`acestor:lifecycle`]|[0].Value}",
                "--output",
                "json",
            ],
            timeout=10,
            stderr=subprocess.DEVNULL,
        )
        return json.loads(out.decode())
    except Exception:
        return []


def _humanise_seconds(s: float) -> str:
    m, sec = divmod(int(s), 60)
    if m == 0:
        return f"{sec}s"
    h, m = divmod(m, 60)
    if h == 0:
        return f"{m}m{sec:02d}s"
    return f"{h}h{m:02d}m"


def _dag_run_id(ledger_row: dict) -> str:
    """Extract '<state>_<yyyy-mm-dd_hhmmss>' style DAG-run key from run_id.

    Best-effort — some ad-hoc runs may not follow the pattern.
    """
    rid = ledger_row.get("run_id", "")
    parts = rid.split("_", 1)
    return parts[1] if len(parts) == 2 else rid


# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    """Home: schedule + running + recent runs."""
    schedule = []
    for state, spec in STATES.items():
        schedule.append(
            {
                "state": state,
                "cron": spec["cron"],
                "next_fire": _next_fire_time(spec["cron"]),
                "tasks": [t.name for t in spec["dag"]],
            }
        )
    running = _running_compute()
    for r in running:
        try:
            launched = datetime.fromisoformat(r["launched"].replace("Z", "+00:00"))
            r["age_min"] = int(
                (datetime.now(timezone.utc) - launched).total_seconds() / 60
            )
        except Exception:
            r["age_min"] = "?"

    ledger = _read_jsonl(LEDGER_PATH, limit=50)
    ledger.reverse()  # newest first
    for row in ledger:
        row["wall_pretty"] = _humanise_seconds(row.get("wall_seconds", 0))

    return TEMPLATES.TemplateResponse(
        "index.html",
        {
            "request": request,
            "schedule": schedule,
            "running": running,
            "ledger": ledger,
            "configs": sorted(p.name for p in CONFIGS_ROOT.glob("*.yaml")),
            "now": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    )


@app.get("/run/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, run_id: str):
    """Single run detail: full ledger row + config + artifact tree."""
    rows = [r for r in _read_jsonl(LEDGER_PATH) if r.get("run_id") == run_id]
    if not rows:
        raise HTTPException(404, f"run_id not found in ledger: {run_id}")
    row = rows[-1]  # last one wins if duplicates
    row["wall_pretty"] = _humanise_seconds(row.get("wall_seconds", 0))

    # Config: path is relative to project root
    config_text = ""
    config_path = REPO_ROOT / row.get("config", "")
    if config_path.is_file():
        try:
            config_text = config_path.read_text()
        except Exception as exc:
            config_text = f"# error reading config: {exc}"

    # Artifacts: try common patterns for locating this run's dir.
    # Naming: <state>-<task>_<stamp>, artifacts land under artifacts/<state>/<run_id>/
    artifact_files: list[dict[str, Any]] = []
    for state_dir in ARTIFACTS_ROOT.iterdir() if ARTIFACTS_ROOT.exists() else []:
        candidate = state_dir / run_id
        if candidate.is_dir():
            for p in sorted(candidate.rglob("*")):
                if p.is_file():
                    rel = p.relative_to(ARTIFACTS_ROOT)
                    artifact_files.append(
                        {
                            "path": str(rel),
                            "size": p.stat().st_size,
                            "size_pretty": _humanise_bytes(p.stat().st_size),
                        }
                    )
            break

    attempts = [
        a
        for a in _read_jsonl(ATTEMPTS_PATH)
        if run_id.startswith(
            f"{a.get('state','')}-{a.get('task','')}_{a.get('dag_run_id','')}"
        )
    ]

    return TEMPLATES.TemplateResponse(
        "run_detail.html",
        {
            "request": request,
            "row": row,
            "config_text": config_text,
            "config_path": (
                str(config_path.relative_to(REPO_ROOT))
                if config_path.is_file()
                else row.get("config", "")
            ),
            "artifact_files": artifact_files,
            "attempts": attempts,
        },
    )


@app.get("/artifacts/{path:path}")
def serve_artifact(path: str, download: int = 0):
    """Serve an artifact file — inline by default, ?download=1 forces download."""
    full = (ARTIFACTS_ROOT / path).resolve()
    if not str(full).startswith(str(ARTIFACTS_ROOT.resolve())):
        raise HTTPException(400, "path escapes artifacts root")
    if not full.is_file():
        raise HTTPException(404, "not found")
    if download:
        return FileResponse(full, filename=full.name)
    # Inline rendering for common text types.
    suffix = full.suffix.lower()
    if suffix in {".csv", ".yaml", ".yml", ".md", ".json", ".log", ".txt"}:
        try:
            return PlainTextResponse(full.read_text(errors="replace"))
        except Exception:
            pass
    return FileResponse(full)


@app.get("/logs/{state_task}/{date}/{fname}")
def serve_log(state_task: str, date: str, fname: str):
    """Serve a scheduler task log for a specific run."""
    p = (LOGS_ROOT / state_task / date / fname).resolve()
    if not str(p).startswith(str(LOGS_ROOT.resolve())):
        raise HTTPException(400, "path escapes logs root")
    if not p.is_file():
        raise HTTPException(404, "not found")
    return PlainTextResponse(p.read_text(errors="replace"))


@app.post("/trigger")
def trigger_run(
    pipeline: str = Form(...),
    config: str = Form(...),
    remote_instance: str = Form("t3.large"),
    remote_lifecycle: str = Form("spot"),
):
    """Spawn acestor.remote as a background subprocess.

    Whitelist-checked against known pipelines + existing config files to
    avoid arbitrary-command injection via the config field.
    """
    allowed_pipelines = {
        "pipelines.dengue_prep.pipeline:build_pipeline",
        "pipelines.dengue.pipeline:build_pipeline",
        "pipelines.dengue_downscale.pipeline:build_pipeline",
        "pipelines.dengue_rollup.pipeline:build_pipeline",
    }
    if pipeline not in allowed_pipelines:
        raise HTTPException(400, f"unknown pipeline: {pipeline}")

    # Config must be a real file under configs/
    cfg_path = (CONFIGS_ROOT / config).resolve()
    if not str(cfg_path).startswith(str(CONFIGS_ROOT.resolve())):
        raise HTTPException(400, "config path escapes configs root")
    if not cfg_path.is_file():
        raise HTTPException(404, f"config not found: {config}")

    if remote_lifecycle not in {"spot", "on-demand"}:
        raise HTTPException(400, "lifecycle must be spot|on-demand")

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    run_id = f"ui-triggered-{cfg_path.stem}_{stamp}"
    log_path = LOGS_ROOT / "ui-triggered" / stamp[:10] / f"{run_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        "-m",
        "acestor.remote",
        "--pipeline",
        pipeline,
        "--config",
        f"configs/{config}",
        "--run-id",
        run_id,
        "--remote-instance",
        remote_instance,
        "--remote-lifecycle",
        remote_lifecycle,
        "--aws-key-name",
        os.environ.get("ACESTOR_AWS_KEY_NAME", "acestor-caller"),
        "--aws-key-path",
        os.environ.get("ACESTOR_AWS_KEY_PATH", "/home/ubuntu/.ssh/acestor-caller.pem"),
    ]
    for env_name in ("DASHBOARD_URL", "DASHBOARD_CLIENT_ID", "DASHBOARD_CLIENT_SECRET"):
        cmd.extend(["--forward-env", env_name])
    ami = os.environ.get("ACESTOR_AWS_AMI")
    if ami:
        cmd.extend(["--aws-ami", ami])

    logf = open(log_path, "w")
    proc = subprocess.Popen(
        cmd,
        stdout=logf,
        stderr=subprocess.STDOUT,
        cwd=str(REPO_ROOT),
        start_new_session=True,  # detach — survives if webui restarts
    )

    return HTMLResponse(
        f"""<html><body style="font-family: system-ui; padding:2em">
        <h2>Triggered</h2>
        <p><strong>run_id:</strong> {run_id}</p>
        <p><strong>pid:</strong> {proc.pid}</p>
        <p><strong>log:</strong> <a href="/logs/ui-triggered/{stamp[:10]}/{run_id}.log">{log_path.relative_to(REPO_ROOT)}</a></p>
        <p><a href="/">← back</a></p>
        </body></html>"""
    )


def _humanise_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("MINI_UI_HOST", "127.0.0.1"),
        port=int(os.environ.get("MINI_UI_PORT", "8000")),
        log_level="info",
    )

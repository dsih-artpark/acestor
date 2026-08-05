"""acestor.webui — FastAPI operator dashboard.

Works for BOTH usage patterns:

* **Local runs** (``acestor.run`` invoked directly): reads artifact dirs
  under ``settings.artifacts_root``. No ledger required.
* **Remote runs** (``acestor.remote``): additionally reads the ledger at
  ``settings.ledger_path`` for rich per-run metadata and cost accounting.

No hard assumptions. Every path is configurable via
``~/.acestor/webui_settings.json`` (editable in-app at ``/settings``).
Missing ledger / configs / scheduler → the relevant sections just render
empty; the UI stays functional.

Access model:
  * Bound to 127.0.0.1 by default (``WEB_UI_HOST`` env override).
  * No built-in auth. On a caller box, reach it via SSM port-forwarding
    (IAM-gated). On a laptop, just open http://localhost:8000/.

Entrypoint: ``python -m acestor.webui``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import secrets as _secrets

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from acestor.webui import settings as _settings
from acestor.webui.settings import Settings

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# `root` is the path prefix (e.g. "/acestor/web-ui" in deploy, "" in local).
# Templates prefix every absolute link with `{{ root }}` so nginx sub-path
# reverse-proxy works without a subdomain.
TEMPLATES.env.globals["root"] = os.environ.get("WEB_UI_ROOT_PATH", "").rstrip("/")


# ── Optional HTTP Basic auth ─────────────────────────────────────────────────
# If both WEB_UI_USERNAME and WEB_UI_PASSWORD are set in the env, every
# request must include matching credentials. If either is unset, auth is
# disabled (fine for laptop dev / behind a trusted network).
_BASIC = HTTPBasic(auto_error=False)


def require_auth(creds: HTTPBasicCredentials | None = Depends(_BASIC)) -> None:
    user_env = os.environ.get("WEB_UI_USERNAME") or ""
    pass_env = os.environ.get("WEB_UI_PASSWORD") or ""
    if not (user_env and pass_env):
        return  # auth disabled
    if creds is None:
        raise HTTPException(
            status_code=401,
            detail="auth required",
            headers={"WWW-Authenticate": "Basic realm=acestor"},
        )
    u_ok = _secrets.compare_digest(creds.username.encode(), user_env.encode())
    p_ok = _secrets.compare_digest(creds.password.encode(), pass_env.encode())
    if not (u_ok and p_ok):
        raise HTTPException(
            status_code=401,
            detail="invalid credentials",
            headers={"WWW-Authenticate": "Basic realm=acestor"},
        )


# App-level dependency: every route gets auth-checked with no per-handler
# boilerplate. When auth-envs are unset, require_auth is a no-op.
# WEB_UI_ROOT_PATH lets nginx reverse-proxy under a sub-path (e.g.
# apps.artpark.ai/acestor/web-ui) without breaking url_for / absolute links.
# Unset in local dev — served at "/" directly.
app = FastAPI(
    title="acestor web-UI",
    docs_url=None,
    redoc_url=None,
    dependencies=[Depends(require_auth)],
    root_path=os.environ.get("WEB_UI_ROOT_PATH", ""),
)


def get_cfg() -> Settings:
    """FastAPI dependency: fresh Settings on every request → hot-editing."""
    return _settings.load()


# ── Optional scheduler discovery ─────────────────────────────────────────────
# The scheduler (scripts/run_schedules_remote.py) is caller-specific ops
# tooling — only present on operator boxes. Import lazily & gracefully so
# acestor.run-only users still see a working UI (empty schedule section).
def _scheduler_states() -> dict[str, Any]:
    for scripts_dir in {
        Path.cwd() / "scripts",
        Path(__file__).resolve().parents[2] / "scripts",
    }:
        if not scripts_dir.exists():
            continue
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
    try:
        from run_schedules_remote import STATES  # type: ignore[import-not-found]

        return STATES
    except Exception:
        return {}


# ── Small helpers ────────────────────────────────────────────────────────────
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
        from apscheduler.triggers.cron import CronTrigger

        trig = CronTrigger.from_crontab(cron, timezone=timezone.utc)
        nxt = trig.get_next_fire_time(None, datetime.now(timezone.utc))
        return nxt.isoformat() if nxt else "-"
    except Exception as exc:
        return f"err: {exc}"


def _humanise_seconds(s: float) -> str:
    m, sec = divmod(int(s), 60)
    if m == 0:
        return f"{sec}s"
    h, m = divmod(m, 60)
    if h == 0:
        return f"{m}m{sec:02d}s"
    return f"{h}h{m:02d}m"


def _humanise_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{int(n)}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def _running_compute(region: str) -> list[dict[str, Any]]:
    """Query AWS for compute EC2s tagged by acestor.remote. Only makes
    sense on a caller with AWS creds; returns [] otherwise (silently)."""
    if not region:
        return []
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
                "launched:LaunchTime,lifecycle:Tags[?Key==`acestor:lifecycle`]|[0].Value,"
                "run_id:Tags[?Key==`acestor:run-id`]|[0].Value}",
                "--output",
                "json",
            ],
            timeout=10,
            stderr=subprocess.DEVNULL,
        )
        return json.loads(out.decode())
    except Exception:
        return []


def _list_local_run_dirs(artifacts_root: Path) -> list[dict[str, Any]]:
    """Enumerate artifact directories that look like run_ids.

    Layout: ``artifacts_root/<pipeline_group>/<run_id>/…``.
    Returns one row per <run_id> — this is how `acestor.run` runs show
    up when there's no remote ledger.
    """
    rows: list[dict[str, Any]] = []
    if not artifacts_root.exists():
        return rows
    for group_dir in sorted(artifacts_root.iterdir()):
        if not group_dir.is_dir():
            continue
        for run_dir in sorted(group_dir.iterdir()):
            if not run_dir.is_dir():
                continue
            stat = run_dir.stat()
            rows.append(
                {
                    "run_id": run_dir.name,
                    "group": group_dir.name,
                    "mtime": datetime.fromtimestamp(
                        stat.st_mtime, timezone.utc
                    ).isoformat(timespec="seconds"),
                    "path": str(run_dir.relative_to(artifacts_root)),
                }
            )
    return rows


def _find_run_artifacts_dir(run_id: str, artifacts_root: Path) -> Path | None:
    """Locate the artifact directory for a given run_id (any group)."""
    if not artifacts_root.exists():
        return None
    for group_dir in artifacts_root.iterdir():
        if group_dir.is_dir() and (group_dir / run_id).is_dir():
            return group_dir / run_id
    return None


def _build_artifact_tree(files: list[dict]) -> dict:
    """Flat list of ``{path, size_pretty}`` → nested ``{dirs, files}``.

    Used by ``run_detail.html`` to render collapsable <details> groups.
    """
    tree: dict = {"dirs": {}, "files": []}
    for f in files:
        parts = f["path"].split("/")
        node = tree
        for p in parts[:-1]:
            node = node["dirs"].setdefault(p, {"dirs": {}, "files": []})
        node["files"].append(
            {"name": parts[-1], "path": f["path"], "size_pretty": f["size_pretty"]}
        )
    return tree


# ── Routes ───────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, cfg: Settings = Depends(get_cfg)):
    ledger_path = cfg.resolved("ledger_path")
    artifacts_root = cfg.resolved("artifacts_root")
    configs_root = cfg.resolved("configs_root")

    # Scheduled DAGs (only shown if scheduler module is discoverable)
    schedule = [
        {
            "state": state,
            "cron": spec["cron"],
            "next_fire": _next_fire_time(spec["cron"]),
            "tasks": [t.name for t in spec["dag"]],
        }
        for state, spec in _scheduler_states().items()
    ]

    # Currently running (AWS query — no-op if creds unavailable)
    running = _running_compute(os.environ.get("AWS_REGION", "ap-south-1"))
    logs_root = cfg.resolved("logs_root")
    for r in running:
        try:
            launched = datetime.fromisoformat(r["launched"].replace("Z", "+00:00"))
            r["age_min"] = int(
                (datetime.now(timezone.utc) - launched).total_seconds() / 60
            )
        except Exception:
            r["age_min"] = "?"
        # Compute log path from run_id (matches scheduler naming convention)
        r["log_relpath"] = ""
        rid = r.get("run_id") or ""
        if rid:
            parts = rid.split("_", 1)
            if len(parts) == 2:
                state_task, stamp = parts
                candidate = logs_root / state_task / stamp[:10] / f"{rid}.log"
                if candidate.is_file():
                    r["log_relpath"] = str(candidate.relative_to(logs_root))

    # Unified runs: ledger rows (rich metadata) + local dirs (bare bones)
    ledger = _read_jsonl(ledger_path, limit=cfg.recent_runs_limit)
    ledger_run_ids = {r.get("run_id") for r in ledger}
    for row in ledger:
        row["wall_pretty"] = _humanise_seconds(row.get("wall_seconds", 0))
        row["source"] = "remote"

    # Local runs = artifact dirs not present in the ledger
    local_rows = [
        {
            "run_id": r["run_id"],
            "group": r["group"],
            "mtime": r["mtime"],
            "source": "local",
        }
        for r in _list_local_run_dirs(artifacts_root)
        if r["run_id"] not in ledger_run_ids
    ]

    # Merge — newest first by started_at (ledger) or mtime (local)
    combined = []
    for row in ledger:
        combined.append({**row, "sort_key": row.get("started_at") or ""})
    for row in local_rows:
        combined.append({**row, "sort_key": row["mtime"]})
    combined.sort(key=lambda r: r["sort_key"], reverse=True)
    combined = combined[: cfg.recent_runs_limit]

    configs = (
        sorted(p.name for p in configs_root.glob("*.yaml"))
        if configs_root.exists()
        else []
    )

    return TEMPLATES.TemplateResponse(
        "index.html",
        {
            "request": request,
            "schedule": schedule,
            "running": running,
            "runs": combined,
            "configs": configs,
            "cfg": cfg,
            "defaults": _settings.defaults_display(),
            "now": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    )


@app.get("/run/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, run_id: str, cfg: Settings = Depends(get_cfg)):
    ledger_path = cfg.resolved("ledger_path")
    attempts_path = cfg.resolved("attempts_path")
    artifacts_root = cfg.resolved("artifacts_root")
    project_root = Path.cwd()

    # Ledger row (if this run was recorded — else None; still show artifacts)
    row = None
    for r in _read_jsonl(ledger_path):
        if r.get("run_id") == run_id:
            row = r
    if row is not None:
        row["wall_pretty"] = _humanise_seconds(row.get("wall_seconds", 0))
        row["source"] = "remote"

    # Config: only if row has one and file exists
    config_text = ""
    config_path_str = ""
    if row and row.get("config"):
        cfg_p = Path(row["config"])
        if not cfg_p.is_absolute():
            cfg_p = (project_root / cfg_p).resolve()
        config_path_str = row["config"]
        if cfg_p.is_file():
            try:
                config_text = cfg_p.read_text()
            except Exception as exc:
                config_text = f"# error reading config: {exc}"

    # Artifacts
    artifact_files: list[dict[str, Any]] = []
    run_dir = _find_run_artifacts_dir(run_id, artifacts_root)
    if run_dir is not None:
        for p in sorted(run_dir.rglob("*")):
            if p.is_file():
                artifact_files.append(
                    {
                        "path": str(p.relative_to(artifacts_root)),
                        "size_pretty": _humanise_bytes(p.stat().st_size),
                    }
                )

    if row is None and not artifact_files:
        raise HTTPException(404, f"no ledger row or artifacts for run_id: {run_id}")

    # Scheduler attempts (only meaningful for scheduler-launched runs)
    attempts = [
        a
        for a in _read_jsonl(attempts_path)
        if run_id.startswith(
            f"{a.get('state','')}-{a.get('task','')}_{a.get('dag_run_id','')}"
        )
    ]

    # Best-effort compute of the scheduler log path for this run.
    # Scheduler naming: <state>-<task>_<yyyy-mm-dd>_<hhmmss>[_r<n>]
    # log lives at logs/<state>-<task>/<yyyy-mm-dd>/<run_id>.log
    scheduler_log_relpath = ""
    logs_root = cfg.resolved("logs_root")
    parts = run_id.split("_", 1)
    if len(parts) == 2:
        state_task, stamp = parts
        candidate = logs_root / state_task / stamp[:10] / f"{run_id}.log"
        if candidate.is_file():
            scheduler_log_relpath = str(candidate.relative_to(logs_root))

    return TEMPLATES.TemplateResponse(
        "run_detail.html",
        {
            "request": request,
            "run_id": run_id,
            "row": row,
            "config_text": config_text,
            "config_path": config_path_str,
            "artifact_files": artifact_files,
            "artifact_tree": _build_artifact_tree(artifact_files),
            "attempts": attempts,
            "cfg": cfg,
            "scheduler_log_relpath": scheduler_log_relpath,
        },
    )


# ── Live log tailing ─────────────────────────────────────────────────────────
def _resolve_log_path(path: str, logs_root: Path) -> Path:
    """Path validation shared by the tail routes."""
    full = (logs_root / path).resolve()
    if not str(full).startswith(str(logs_root)):
        raise HTTPException(400, "path escapes logs root")
    if not full.is_file():
        raise HTTPException(404, "not found")
    return full


@app.get("/log-view", response_class=HTMLResponse)
def log_view(request: Request, path: str, cfg: Settings = Depends(get_cfg)):
    """HTML page that live-tails the given log file (polling every 2s)."""
    logs_root = cfg.resolved("logs_root")
    full = _resolve_log_path(path, logs_root)
    return TEMPLATES.TemplateResponse(
        "tail_log.html",
        {"request": request, "path": path, "filename": full.name},
    )


@app.get("/log-raw", response_class=PlainTextResponse)
def log_raw(path: str, tail_kb: int = 128, cfg: Settings = Depends(get_cfg)):
    """Return the last ``tail_kb`` KiB of the log file. Used by the poller.

    Capped at 1 MiB per fetch — larger tails would defeat the purpose.
    """
    logs_root = cfg.resolved("logs_root")
    full = _resolve_log_path(path, logs_root)
    tail_bytes = max(1, min(tail_kb, 1024)) * 1024
    size = full.stat().st_size
    start = max(0, size - tail_bytes)
    with open(full, "rb") as f:
        f.seek(start)
        data = f.read()
    text = data.decode(errors="replace")
    # If we started mid-line, drop the first partial line for readability.
    if start > 0 and "\n" in text:
        text = text.split("\n", 1)[1]
    return text


@app.get("/artifacts/{path:path}")
def serve_artifact(path: str, download: int = 0, cfg: Settings = Depends(get_cfg)):
    root = cfg.resolved("artifacts_root")
    full = (root / path).resolve()
    if not str(full).startswith(str(root)):
        raise HTTPException(400, "path escapes artifacts root")
    if not full.is_file():
        raise HTTPException(404, "not found")
    if download:
        return FileResponse(full, filename=full.name)
    if full.suffix.lower() in {".csv", ".yaml", ".yml", ".md", ".json", ".log", ".txt"}:
        try:
            return PlainTextResponse(full.read_text(errors="replace"))
        except Exception:
            pass
    return FileResponse(full)


@app.post("/trigger")
def trigger_run(
    mode: str = Form("remote"),  # "local" (acestor.run) or "remote" (acestor.remote)
    pipeline: str = Form(...),
    config: str = Form(...),
    remote_instance: str = Form("t3.large"),
    remote_lifecycle: str = Form("spot"),
    cfg: Settings = Depends(get_cfg),
):
    """Spawn a run in the background — either local (acestor.run) or remote."""
    allowed_pipelines = {
        "pipelines.dengue_prep.pipeline:build_pipeline",
        "pipelines.dengue.pipeline:build_pipeline",
        "pipelines.dengue_downscale.pipeline:build_pipeline",
        "pipelines.dengue_rollup.pipeline:build_pipeline",
    }
    if pipeline not in allowed_pipelines:
        raise HTTPException(400, f"unknown pipeline: {pipeline}")
    if mode not in {"local", "remote"}:
        raise HTTPException(400, f"mode must be local|remote, got {mode!r}")

    configs_root = cfg.resolved("configs_root")
    cfg_path = (configs_root / config).resolve()
    if not str(cfg_path).startswith(str(configs_root)):
        raise HTTPException(400, "config path escapes configs root")
    if not cfg_path.is_file():
        raise HTTPException(404, f"config not found: {config}")

    logs_root = cfg.resolved("logs_root")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    run_id = f"ui-{mode}-{cfg_path.stem}_{stamp}"
    log_path = logs_root / "ui-triggered" / stamp[:10] / f"{run_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    if mode == "local":
        cmd = [
            sys.executable,
            "-m",
            "acestor.run",
            "--pipeline",
            pipeline,
            "--config",
            str(cfg_path),
            "--run-id",
            run_id,
        ]
    else:
        if remote_lifecycle not in {"spot", "on-demand"}:
            raise HTTPException(400, "lifecycle must be spot|on-demand")
        cmd = [
            sys.executable,
            "-m",
            "acestor.remote",
            "--pipeline",
            pipeline,
            "--config",
            str(cfg_path),
            "--run-id",
            run_id,
            "--remote-instance",
            remote_instance,
            "--remote-lifecycle",
            remote_lifecycle,
            "--aws-key-name",
            os.environ.get("ACESTOR_AWS_KEY_NAME", "acestor-caller"),
            "--aws-key-path",
            os.environ.get(
                "ACESTOR_AWS_KEY_PATH", str(Path.home() / ".ssh" / "acestor-caller.pem")
            ),
        ]
        for env_name in (
            "DASHBOARD_URL",
            "DASHBOARD_CLIENT_ID",
            "DASHBOARD_CLIENT_SECRET",
        ):
            cmd.extend(["--forward-env", env_name])
        ami = os.environ.get("ACESTOR_AWS_AMI")
        if ami:
            cmd.extend(["--aws-ami", ami])

    logf = open(log_path, "w")
    proc = subprocess.Popen(
        cmd,
        stdout=logf,
        stderr=subprocess.STDOUT,
        cwd=os.getcwd(),
        start_new_session=True,
    )

    return HTMLResponse(
        f"""<html><body style="font-family:system-ui;padding:2em">
        <h2>Triggered · {mode}</h2>
        <p><strong>run_id:</strong> {run_id}</p>
        <p><strong>pid:</strong> {proc.pid}</p>
        <p><strong>log:</strong> <code>{log_path}</code></p>
        <p><a href="/">← back</a></p>
        </body></html>"""
    )


# ── Settings routes ──────────────────────────────────────────────────────────
@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, cfg: Settings = Depends(get_cfg)):
    return TEMPLATES.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "cfg": cfg,
            "defaults": _settings.defaults_display(),
        },
    )


@app.post("/settings")
def settings_save(
    artifacts_root: str = Form(""),
    configs_root: str = Form(""),
    logs_root: str = Form(""),
    ledger_path: str = Form(""),
    attempts_path: str = Form(""),
    recent_runs_limit: int = Form(50),
    auto_refresh_sec: int = Form(30),
    default_instance: str = Form("t3.large"),
    default_lifecycle: str = Form("spot"),
):
    """Persist edited settings to disk. Blank strings = revert to default."""
    _settings.save(
        Settings(
            artifacts_root=artifacts_root.strip(),
            configs_root=configs_root.strip(),
            logs_root=logs_root.strip(),
            ledger_path=ledger_path.strip(),
            attempts_path=attempts_path.strip(),
            recent_runs_limit=max(1, recent_runs_limit),
            auto_refresh_sec=max(0, auto_refresh_sec),
            default_instance=default_instance.strip() or "t3.large",
            default_lifecycle=default_lifecycle.strip() or "spot",
        )
    )
    return RedirectResponse("/settings", status_code=303)

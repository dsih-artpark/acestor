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

import html
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
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
_ROOT = os.environ.get("WEB_UI_ROOT_PATH", "").rstrip("/")
TEMPLATES.env.globals["root"] = _ROOT


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
def dashboard(
    request: Request,
    page: int = 1,
    cfg: Settings = Depends(get_cfg),
):
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
        r["log_relpath"] = _find_log_relpath(r.get("run_id") or "", logs_root)

    # Unified runs: ledger rows (rich metadata) + local dirs (bare bones).
    # Read WITHOUT the limit — we paginate in memory below so the user can
    # navigate to older runs. The ledger is a few thousand rows max in
    # practice, so full-read per page-render is fine.
    ledger = _read_jsonl(ledger_path)
    ledger_run_ids = {r.get("run_id") for r in ledger}
    for row in ledger:
        row["wall_pretty"] = _humanise_seconds(row.get("wall_seconds", 0))
        row["source"] = "remote"
        row["log_relpath"] = _find_log_relpath(row.get("run_id") or "", logs_root)

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

    # Paginate
    per_page = max(1, cfg.recent_runs_limit)
    total = len(combined)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    page_rows = combined[start : start + per_page]
    pagination = {
        "page": page,
        "total_pages": total_pages,
        "total": total,
        "per_page": per_page,
        "start": start + 1 if page_rows else 0,
        "end": start + len(page_rows),
        "has_prev": page > 1,
        "has_next": page < total_pages,
    }

    configs = (
        sorted(p.name for p in configs_root.glob("*.yaml"))
        if configs_root.exists()
        else []
    )

    return TEMPLATES.TemplateResponse(
        request,
        "index.html",
        {
            "request": request,
            "schedule": schedule,
            "running": running,
            "runs": page_rows,
            "pagination": pagination,
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

    logs_root = cfg.resolved("logs_root")
    scheduler_log_relpath = _find_log_relpath(run_id, logs_root)

    # Pre-fill values for the Push-to-Dashboard button (only meaningful when
    # the run produced a predictions.csv — the template checks for that).
    push_hints = {"scope_id": "", "reference_date": "", "has_predictions": False}
    predictions_csv_path: Path | None = None
    for f in artifact_files:
        if f["path"].endswith("outputs/predictions.csv"):
            push_hints["has_predictions"] = True
            group = f["path"].split("/", 1)[0]
            push_hints["scope_id"] = _detect_scope_id(group)
            # reference_date: derive from run_id's trailing YYYY-MM-DD_HHMMSS,
            # snapped to Monday of that week.
            m = re.search(r"(\d{4}-\d{2}-\d{2})_\d{6}$", run_id)
            if m:
                push_hints["reference_date"] = _monday_of(m.group(1))
            predictions_csv_path = (artifacts_root / f["path"]).resolve()
            break

    # 4-week aggregate cards (like the dashboard's hero) — sum predictions
    # across all regions per (target_week) for the ensemble/historical rows.
    prediction_cards = _prediction_week_cards(predictions_csv_path)
    # Per-model comparison: which region/week had the largest disagreement
    # across ensemble members? Reads sibling per_model/*.csv files.
    model_disagreements = _model_disagreements(predictions_csv_path)

    return TEMPLATES.TemplateResponse(
        request,
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
            "push_hints": push_hints,
            "prediction_cards": prediction_cards,
            "model_disagreements": model_disagreements,
        },
    )


def _prediction_week_cards(csv_path: Path | None) -> list[dict]:
    """Aggregate ensemble/historical predictions per target_week for cards.

    Returns [{week, week_label, total, total_min, total_max, n_regions}, ...]
    ordered by week ascending. Empty list if no CSV or no matching rows.
    Reads only the columns we need — no pandas dep for a page render.
    """
    if csv_path is None or not csv_path.is_file():
        return []
    import csv as _csv
    from collections import defaultdict

    per_week: dict[str, dict] = defaultdict(
        lambda: {"total": 0.0, "min": 0.0, "max": 0.0, "n": 0}
    )
    try:
        with csv_path.open("r", newline="") as f:
            reader = _csv.DictReader(f)
            for row in reader:
                # downscale / rollup CSVs share the same column names as
                # forecast, so this works uniformly.
                if row.get("model") not in ("ensembleModel", "", None):
                    continue
                if row.get("thresholdMethod") not in ("historical", "", None):
                    continue
                wk = row.get("startDatePredictedWeek") or row.get("target_week")
                if not wk:
                    continue

                def _f(v: str) -> float:
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        return 0.0

                pred = _f(row.get("predictionRaw") or row.get("prediction") or "0")
                lo = _f(row.get("predictionMin") or row.get("prediction_min") or pred)
                hi = _f(row.get("predictionMax") or row.get("prediction_max") or pred)
                per_week[wk]["total"] += pred
                per_week[wk]["min"] += lo
                per_week[wk]["max"] += hi
                per_week[wk]["n"] += 1
    except OSError:
        return []

    def _label(wk: str) -> str:
        try:
            start = datetime.fromisoformat(wk).date()
        except (ValueError, TypeError):
            return wk
        end = start.fromordinal(start.toordinal() + 6)
        # "10 Aug – 16 Aug 2026"
        return f"{start.strftime('%d %b')} – {end.strftime('%d %b %Y')}"

    return [
        {
            "week": wk,
            "week_label": _label(wk),
            "total": round(v["total"]),
            "total_min": round(v["min"]),
            "total_max": round(v["max"]),
            "n_regions": v["n"],
        }
        for wk, v in sorted(per_week.items())
    ]


def _model_disagreements(
    predictions_csv_path: Path | None, top_n: int = 20
) -> list[dict]:
    """Return top rows where the ensemble members disagreed most.

    Reads sibling ``per_model/predictions_<m>.csv`` files (RF, TimesFM, XGB…)
    and joins them on ``(regionID, startDatePredictedWeek)`` for the
    historical thresholdMethod. Each returned row has:

        {region, week, models: {rf: X, timesfm: Y, ...}, ensemble: E,
         spread: max-min, ratio: max/max(min,1) — how many times}

    Sorted by ``ratio`` descending (biggest disagreements first). Empty
    list if there's no ``per_model/`` sibling.
    """
    if predictions_csv_path is None or not predictions_csv_path.is_file():
        return []
    pm_dir = predictions_csv_path.parent / "per_model"
    if not pm_dir.is_dir():
        return []
    import csv as _csv

    # Load per-model raw predictions (only historical/latest weeks).
    # per_model/predictions_<model>.csv has same columns as main predictions.csv.
    per_model: dict[tuple[str, str], dict[str, float]] = {}
    for path in sorted(pm_dir.glob("predictions_*.csv")):
        model = path.stem.replace("predictions_", "")
        try:
            with path.open("r", newline="") as f:
                for row in _csv.DictReader(f):
                    if row.get("thresholdMethod") not in ("historical", "", None):
                        continue
                    key = (
                        row.get("regionID") or "",
                        row.get("startDatePredictedWeek") or "",
                    )
                    if not all(key):
                        continue
                    try:
                        val = float(
                            row.get("predictionRaw") or row.get("prediction") or "0"
                        )
                    except ValueError:
                        continue
                    per_model.setdefault(key, {})[model] = val
        except OSError:
            continue
    # Also load the ensemble from the main predictions.csv for the same keys.
    ensembles: dict[tuple[str, str], float] = {}
    try:
        with predictions_csv_path.open("r", newline="") as f:
            for row in _csv.DictReader(f):
                if row.get("model") != "ensembleModel":
                    continue
                if row.get("thresholdMethod") not in ("historical", "", None):
                    continue
                key = (
                    row.get("regionID") or "",
                    row.get("startDatePredictedWeek") or "",
                )
                try:
                    ensembles[key] = float(
                        row.get("predictionRaw") or row.get("prediction") or "0"
                    )
                except ValueError:
                    pass
    except OSError:
        pass

    rows: list[dict] = []
    for (region, week), models in per_model.items():
        if len(models) < 2:
            continue
        vals = list(models.values())
        lo, hi = min(vals), max(vals)
        spread = hi - lo
        ratio = hi / max(lo, 0.5)  # avoid div-by-zero on near-zero mins
        rows.append(
            {
                "region": region,
                "week": week,
                "models": models,
                "ensemble": ensembles.get((region, week)),
                "spread": round(spread, 2),
                "ratio": round(ratio, 1),
            }
        )
    rows.sort(key=lambda r: r["ratio"], reverse=True)
    return rows[:top_n]


# ── Live log tailing ─────────────────────────────────────────────────────────
def _find_log_relpath(run_id: str, logs_root: Path) -> str:
    """Locate the log file for a run_id under logs_root.

    Two layouts to check:
      - scheduler:   <state>-<task>/<yyyy-mm-dd>/<run_id>.log
      - ui-trigger:  ui-triggered/<yyyy-mm-dd>/<run_id>.log
    Falls back to a recursive glob if the run_id's date can't be parsed
    from its suffix.
    """
    if not run_id or not logs_root.exists():
        return ""
    # UI-triggered runs: run_id = "ui-<mode>-<stem>_<yyyy-mm-dd>_<hhmmss>"
    m = re.search(r"(\d{4}-\d{2}-\d{2})_\d{6}$", run_id)
    date = m.group(1) if m else ""
    candidates: list[Path] = []
    if date:
        candidates.append(logs_root / "ui-triggered" / date / f"{run_id}.log")
        # Scheduler naming: <state>-<task>_<yyyy-mm-dd>_<hhmmss>[_r<n>]
        parts = run_id.split("_", 1)
        if len(parts) == 2:
            candidates.append(logs_root / parts[0] / date / f"{run_id}.log")
    for c in candidates:
        if c.is_file():
            return str(c.relative_to(logs_root))
    # Fallback: recursive glob — cheap since logs_root has small fanout
    for match in logs_root.rglob(f"{run_id}.log"):
        return str(match.relative_to(logs_root))
    return ""


def _resolve_log_path(path: str, logs_root: Path) -> Path:
    """Path validation shared by the tail routes."""
    full = (logs_root / path).resolve()
    if not str(full).startswith(str(logs_root)):
        raise HTTPException(400, "path escapes logs root")
    if not full.is_file():
        raise HTTPException(404, "not found")
    return full


@app.get("/log-view", response_class=HTMLResponse)
def log_view(
    request: Request,
    path: str,
    run_id: str = "",
    cfg: Settings = Depends(get_cfg),
):
    """HTML page that live-tails the given log file (polling every 2s).

    If ``run_id`` is provided AND ``metrics.jsonl`` exists in the run's
    artifact directory, the page renders a compact system-metrics strip
    (CPU / memory / disk) above the log tail. Absence of metrics is silently
    tolerated — the strip is hidden.
    """
    logs_root = cfg.resolved("logs_root")
    full = _resolve_log_path(path, logs_root)
    show_metrics = False
    if run_id:
        artifacts_root = cfg.resolved("artifacts_root")
        run_dir = _find_run_artifacts_dir(run_id, artifacts_root)
        show_metrics = bool(run_dir and (run_dir / "metrics.jsonl").is_file())
    return TEMPLATES.TemplateResponse(
        request,
        "tail_log.html",
        {
            "request": request,
            "path": path,
            "filename": full.name,
            "run_id": run_id,
            "show_metrics": show_metrics,
        },
    )


@app.get("/metrics-raw")
def metrics_raw(
    run_id: str,
    tail_n: int = 720,
    cfg: Settings = Depends(get_cfg),
):
    """Return the last ``tail_n`` samples from a run's ``metrics.jsonl``.

    Default 720 samples ≈ 60 min at the 5s writer cadence — enough for a
    full forecast run without exploding the browser side. Capped at 5000.
    """
    artifacts_root = cfg.resolved("artifacts_root")
    run_dir = _find_run_artifacts_dir(run_id, artifacts_root)
    if run_dir is None:
        raise HTTPException(404, f"no artifacts dir for run_id: {run_id}")
    metrics_file = run_dir / "metrics.jsonl"
    if not metrics_file.is_file():
        return {"samples": []}
    keep = max(1, min(tail_n, 5000))
    # Read the last N lines cheaply — metrics.jsonl is append-only + small
    # (5s samples × 60 min ≈ 40 KB). Full read is fine at these sizes.
    lines = metrics_file.read_text(errors="replace").splitlines()[-keep:]
    samples: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            samples.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return {"samples": samples}


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
def serve_artifact(
    request: Request,
    path: str,
    download: int = 0,
    cfg: Settings = Depends(get_cfg),
):
    root = cfg.resolved("artifacts_root")
    full = (root / path).resolve()
    if not str(full).startswith(str(root)):
        raise HTTPException(400, "path escapes artifacts root")
    if not full.is_file():
        raise HTTPException(404, "not found")
    if download:
        return FileResponse(full, filename=full.name)
    suffix = full.suffix.lower()
    if suffix == ".csv":
        # Render CSV as a scrollable HTML table (first 1000 rows).
        return _render_csv_page(request, full, path)
    if suffix == ".html":
        # Report / brief HTML — serve as HTML so the browser renders it.
        try:
            return HTMLResponse(full.read_text(errors="replace"))
        except Exception:
            pass
    if suffix in {".yaml", ".yml", ".md", ".json", ".log", ".txt"}:
        try:
            return PlainTextResponse(full.read_text(errors="replace"))
        except Exception:
            pass
    return FileResponse(full)


# ── CSV → HTML table (in-browser preview, capped rows) ──────────────────────
_CSV_ROW_CAP = 1000


def _render_csv_page(request: Request, full: Path, relpath: str) -> HTMLResponse:
    import csv as _csv

    rows: list[list[str]] = []
    truncated = False
    try:
        with full.open("r", encoding="utf-8", errors="replace", newline="") as f:
            reader = _csv.reader(f)
            for i, row in enumerate(reader):
                if i >= _CSV_ROW_CAP + 1:  # +1 for header
                    truncated = True
                    break
                rows.append(row)
    except Exception as exc:
        return PlainTextResponse(f"failed to parse CSV: {exc}", status_code=500)
    header = rows[0] if rows else []
    body = rows[1:] if rows else []
    return TEMPLATES.TemplateResponse(
        request,
        "csv_view.html",
        {
            "request": request,
            "relpath": relpath,
            "header": header,
            "body": body,
            "row_count": len(body),
            "truncated": truncated,
            "cap": _CSV_ROW_CAP,
        },
    )


# ── Datasets browser ─────────────────────────────────────────────────────────
# Shared per-state dataset dirs (`ka_datasets/`, `gba_datasets/`, …) that
# `acestor.remote` rsyncs back after prep runs. Not run-scoped — files from
# many runs land here — but this is the fastest way to grab the prepared
# CSVs / hyperparam JSONs from the browser.
def _datasets_project_root() -> Path:
    return Path.cwd()


def _list_dataset_roots() -> list[Path]:
    root = _datasets_project_root()
    return sorted(p for p in root.glob("*_datasets") if p.is_dir())


def _resolve_dataset_path(rel: str) -> Path:
    """Validate `rel` stays inside one of the *_datasets dirs at project root."""
    project = _datasets_project_root().resolve()
    full = (project / rel).resolve()
    if not str(full).startswith(str(project) + os.sep) and full != project:
        raise HTTPException(400, "path escapes project root")
    # First segment must be a *_datasets dir
    try:
        first = full.relative_to(project).parts[0]
    except (ValueError, IndexError):
        raise HTTPException(400, "path outside datasets") from None
    if not first.endswith("_datasets"):
        raise HTTPException(400, "path outside datasets") from None
    return full


def _dir_stats(p: Path, cap: int = 5000) -> tuple[int, int]:
    """Return (file_count, total_bytes) under ``p``. Caps at ``cap`` files."""
    n = 0
    total = 0
    for f in p.rglob("*"):
        if n >= cap:
            break
        try:
            if f.is_file():
                total += f.stat().st_size
                n += 1
        except OSError:
            continue
    return n, total


def _dataset_dir_entries(p: Path) -> list[dict]:
    entries = []
    for child in sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name)):
        try:
            stat = child.stat()
        except OSError:
            continue
        if child.is_dir():
            n_files, total = _dir_stats(child)
        else:
            n_files, total = 1, stat.st_size
        entries.append(
            {
                "name": child.name,
                "is_dir": child.is_dir(),
                "size": stat.st_size if child.is_file() else total,
                "size_pretty": _humanise_bytes(
                    total if child.is_dir() else stat.st_size
                ),
                "file_count": n_files,
                "mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(
                    timespec="seconds"
                ),
            }
        )
    return entries


def _state_prefix_of(rel: str) -> str:
    """Return the state prefix of a datasets-relative path.

    e.g. 'gba_datasets/prepared_data/zone' → 'gba'
    """
    first = rel.split("/", 1)[0]
    if first.endswith("_datasets"):
        return first[: -len("_datasets")]
    return ""


def _running_state_prefixes(region: str) -> set[str]:
    """Which state prefixes have a compute EC2 running right now.

    Extracts from run_id tags: scheduler runs like ``gba-zone-forecast_...``
    → 'gba'; UI runs like ``ui-remote-gba_zone_prep_...`` → 'gba'.
    """
    prefixes: set[str] = set()
    for r in _running_compute(region):
        rid = (r.get("run_id") or "").lower()
        if not rid:
            continue
        if rid.startswith("ui-"):
            # ui-<mode>-<config-stem>_<date>_<time>
            m = re.match(r"ui-[a-z]+-([a-z]+)", rid)
            if m:
                prefixes.add(m.group(1))
        else:
            # scheduler: <state>-<task>_<date>_<time>
            m = re.match(r"([a-z]+)-", rid)
            if m:
                prefixes.add(m.group(1))
    return prefixes


@app.get("/datasets", response_class=HTMLResponse)
def datasets_index(request: Request, cfg: Settings = Depends(get_cfg)):
    roots = _list_dataset_roots()
    project = _datasets_project_root().resolve()
    top = [
        {
            "name": r.name,
            "relpath": str(r.relative_to(project)),
        }
        for r in roots
    ]
    return TEMPLATES.TemplateResponse(
        request,
        "datasets.html",
        {
            "request": request,
            "top": top,
            "here": "",
            "parent": "",
            "entries": [],
            "cfg": cfg,
        },
    )


@app.get("/datasets/{path:path}")
def datasets_browse(
    request: Request,
    path: str,
    download: int = 0,
    cfg: Settings = Depends(get_cfg),
):
    full = _resolve_dataset_path(path)
    if full.is_file():
        if download:
            return FileResponse(full, filename=full.name)
        if full.suffix.lower() == ".csv":
            return _render_csv_page(request, full, path)
        if full.suffix.lower() in {".yaml", ".yml", ".md", ".json", ".log", ".txt"}:
            try:
                return PlainTextResponse(full.read_text(errors="replace"))
            except Exception:
                pass
        return FileResponse(full)
    # Directory — render browser
    project = _datasets_project_root().resolve()
    here = str(full.relative_to(project))
    entries = _dataset_dir_entries(full)
    parent = str(full.parent.relative_to(project)) if full != project else ""
    top = [
        {"name": r.name, "relpath": str(r.relative_to(project))}
        for r in _list_dataset_roots()
    ]
    return TEMPLATES.TemplateResponse(
        request,
        "datasets.html",
        {
            "request": request,
            "top": top,
            "here": here,
            "parent": parent,
            "entries": entries,
            "cfg": cfg,
        },
    )


@app.get("/new-run", response_class=HTMLResponse)
def run_new_page(request: Request, cfg: Settings = Depends(get_cfg)):
    """Dedicated trigger page with YAML editor for the selected config."""
    configs_root = cfg.resolved("configs_root")
    configs = (
        sorted(p.name for p in configs_root.glob("*.yaml"))
        if configs_root.exists()
        else []
    )
    return TEMPLATES.TemplateResponse(
        request,
        "run_new.html",
        {"request": request, "configs": configs, "cfg": cfg},
    )


@app.get("/config-yaml")
def config_yaml(name: str, cfg: Settings = Depends(get_cfg)):
    """Return the raw YAML of a config for the editor to load."""
    configs_root = cfg.resolved("configs_root")
    full = (configs_root / name).resolve()
    if not str(full).startswith(str(configs_root)):
        raise HTTPException(400, "path escapes configs root")
    if not full.is_file():
        raise HTTPException(404, "not found")
    return PlainTextResponse(full.read_text())


@app.post("/datasets-delete")
def datasets_delete(path: str = Form(...)):
    """Delete a file or a directory (recursive) inside a *_datasets/ tree.

    Refuses to delete:
      * the top-level *_datasets/ folder itself (data-root safety)
      * anything while a compute EC2 for the same state prefix is running
        (would race with an in-flight pipeline)
    No undo.
    """
    import shutil

    full = _resolve_dataset_path(path)
    project = _datasets_project_root().resolve()
    rel = full.relative_to(project)
    parts = rel.parts
    if len(parts) < 2:
        raise HTTPException(400, "refusing to delete a top-level *_datasets/ folder")
    state = _state_prefix_of(str(rel))
    running = _running_state_prefixes(os.environ.get("AWS_REGION", "ap-south-1"))
    if state in running:
        raise HTTPException(
            409,
            f"refusing: a compute EC2 for state={state!r} is running — wait "
            f"for it to finish (or terminate it) before deleting under "
            f"{parts[0]}/",
        )
    if not full.exists():
        raise HTTPException(404, "not found")
    if full.is_dir():
        shutil.rmtree(full)
    else:
        full.unlink()
    parent = str(rel.parent)
    return RedirectResponse(f"{_ROOT}/datasets/{parent}", status_code=303)


@app.post("/trigger")
def trigger_run(
    mode: str = Form("remote"),  # "local" (acestor.run) or "remote" (acestor.remote)
    pipeline: str = Form(...),
    config: str = Form(...),
    remote_instance: str = Form("t3.large"),
    remote_lifecycle: str = Form("spot"),
    yaml_override: str = Form(""),
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

    # If the user edited the YAML in the browser, validate + save to a
    # sibling _user_edits/ tree and launch from there. Never overwrites the
    # source config so rollback is trivial.
    if yaml_override.strip():
        original = cfg_path.read_text()
        if yaml_override.strip() != original.strip():
            import yaml as _yaml

            try:
                _yaml.safe_load(yaml_override)
            except _yaml.YAMLError as exc:
                raise HTTPException(400, f"invalid YAML: {exc}") from None
            edit_dir = configs_root / "_user_edits"
            edit_dir.mkdir(parents=True, exist_ok=True)
            new_name = f"{cfg_path.stem}_{stamp}.yaml"
            new_path = edit_dir / new_name
            new_path.write_text(yaml_override)
            cfg_path = new_path
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
        # Compute box rsyncs the repo to a different absolute path
        # (e.g. /home/ubuntu/acestor) than the caller
        # (/home/ubuntu/acestor-work), so absolute caller-side paths break
        # remote resolution. Pass a repo-root-relative path instead —
        # acestor.remote cd's into its workspace before running.
        try:
            remote_config = str(cfg_path.relative_to(configs_root.parent))
        except ValueError:
            remote_config = str(cfg_path)
        cmd = [
            sys.executable,
            "-m",
            "acestor.remote",
            "--pipeline",
            pipeline,
            "--config",
            remote_config,
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
    # Persist run_id → pid so /cancel-run can kill the caller-side
    # subprocess in addition to terminating the compute EC2.
    _record_running(
        run_id=run_id,
        pid=proc.pid,
        mode=mode,
        config=str(cfg_path),
        pipeline=pipeline,
    )

    # Jump straight to the live log viewer for this run. The tail page polls
    # every 2s, so the user watches output stream in as the pipeline runs.
    # Pass run_id so the tail page can hydrate the metrics strip too.
    log_relpath = str(log_path.relative_to(logs_root))
    return RedirectResponse(
        f"{_ROOT}/log-view?path={log_relpath}&run_id={urllib.parse.quote(run_id)}",
        status_code=303,
    )


# ── Run cancellation ─────────────────────────────────────────────────────────
# Per-trigger PID log so /cancel-run can kill the caller-side subprocess
# that spawned the compute EC2. EC2 termination alone is not enough: the
# caller-side acestor.remote will keep retrying rsync against the dead
# host for a minute or two before giving up.
_RUNNING_PATH = Path.home() / ".acestor" / "webui_running.jsonl"


def _record_running(
    *, run_id: str, pid: int, mode: str, config: str, pipeline: str
) -> None:
    _RUNNING_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "run_id": run_id,
        "pid": pid,
        "mode": mode,
        "config": config,
        "pipeline": pipeline,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    with open(_RUNNING_PATH, "a") as f:
        f.write(json.dumps(row) + "\n")


def _find_pid_for_run(run_id: str) -> int | None:
    """Return the most recent PID we recorded for run_id (or None)."""
    if not _RUNNING_PATH.exists():
        return None
    hit = None
    for row in _read_jsonl(_RUNNING_PATH):
        if row.get("run_id") == run_id:
            hit = row.get("pid")
    return int(hit) if hit else None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _pid_looks_like_ours(pid: int, run_id: str) -> bool:
    """Verify the PID actually belongs to the acestor.remote subprocess we
    launched — guards against PID recycling on a long-lived caller box.

    We stamped run_id into the command line via ``--run-id``, so an intact
    child still shows both markers in ``/proc/<pid>/cmdline``.
    """
    try:
        cmdline = (
            Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", errors="replace")
        )
    except OSError:
        return False
    return "acestor" in cmdline and run_id in cmdline


def _kill_pid(pid: int) -> str:
    """SIGTERM the process group, wait 3s, SIGKILL if still alive."""
    import signal
    import time

    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError) as exc:
        return f"no such process ({exc.__class__.__name__})"
    for _ in range(15):  # 3s
        if not _pid_alive(pid):
            return "SIGTERM ok"
        time.sleep(0.2)
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    return "SIGKILL forced"


def _terminate_ec2_for_run(run_id: str, region: str) -> str:
    """Terminate any running compute EC2 tagged with acestor:run-id=<run_id>."""
    if not region:
        return "no region"
    try:
        out = subprocess.check_output(
            [
                "aws",
                "ec2",
                "describe-instances",
                "--region",
                region,
                "--filters",
                f"Name=tag:acestor:run-id,Values={run_id}",
                "Name=instance-state-name,Values=running,pending",
                "--query",
                "Reservations[].Instances[].InstanceId",
                "--output",
                "json",
            ],
            timeout=10,
            stderr=subprocess.DEVNULL,
        )
        ids = json.loads(out.decode())
    except Exception as exc:
        return f"describe-instances failed: {exc}"
    if not ids:
        return "no matching EC2"
    try:
        subprocess.check_call(
            [
                "aws",
                "ec2",
                "terminate-instances",
                "--region",
                region,
                "--instance-ids",
                *ids,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
    except Exception as exc:
        return f"terminate-instances failed: {exc}"
    return f"terminated {', '.join(ids)}"


@app.post("/cancel-run")
def cancel_run(run_id: str = Form(...)):
    """Cancel a triggered run: kill caller subprocess + terminate compute EC2.

    Both actions run best-effort and report their outcome. If either
    succeeded, the pipeline is effectively cancelled.
    """
    region = os.environ.get("AWS_REGION", "ap-south-1")
    pid = _find_pid_for_run(run_id)
    pid_msg = "no recorded pid"
    if pid is not None:
        if not _pid_alive(pid):
            pid_msg = f"pid {pid} already dead"
        elif not _pid_looks_like_ours(pid, run_id):
            # PID has been recycled by an unrelated process — SIGTERM'ing
            # it would kill something we don't own. EC2 termination below
            # will still cancel the run.
            pid_msg = f"pid {pid} recycled (not our process) — skipping kill"
        else:
            pid_msg = f"pid {pid}: {_kill_pid(pid)}"
    ec2_msg = _terminate_ec2_for_run(run_id, region)
    return HTMLResponse(
        f"""<html><body style="font-family:system-ui;padding:2em">
        <h2>Cancel · {run_id}</h2>
        <p><strong>caller subprocess:</strong> {pid_msg}</p>
        <p><strong>compute EC2:</strong> {ec2_msg}</p>
        <p><a href="{_ROOT}/">← back to dashboard</a></p>
        </body></html>"""
    )


# ── Trigger a scheduler DAG on-demand ────────────────────────────────────────
# Fires _run_dag(state, ...) from scripts/run_schedules_remote.py in a
# background python subprocess so the HTTP response returns immediately.
# The DAG runner logs to logs/manual-dag-<state>-<stamp>.log; the caller's
# systemd scheduler is untouched (parallel runs are safe — each task uses
# its own run_id timestamp).
_STATE_RE = re.compile(r"^[a-z0-9_]+$")


@app.post("/trigger-dag")
def trigger_dag(state: str = Form(...)):
    if not _STATE_RE.match(state):
        raise HTTPException(400, f"invalid state: {state!r}")
    states = _scheduler_states()
    if state not in states:
        raise HTTPException(
            404,
            f"unknown state {state!r} — known: {sorted(states)}",
        )
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    log_path = Path.cwd() / "logs" / "manual-dag" / f"{state}_{stamp}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    scripts_dir = str(Path.cwd() / "scripts")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sys; sys.path.insert(0, " + repr(scripts_dir) + "); "
                "from run_schedules_remote import STATES, _run_dag; "
                "_run_dag(" + repr(state) + ", STATES[" + repr(state) + "]['dag'])"
            ),
        ],
        stdout=open(log_path, "w"),
        stderr=subprocess.STDOUT,
        cwd=os.getcwd(),
        start_new_session=True,
    )
    e = html.escape
    log_relpath = str(log_path.relative_to(Path.cwd() / "logs"))
    return HTMLResponse(
        f"""<html><body style="font-family:system-ui;padding:2em">
        <h2>DAG triggered · {e(state)}</h2>
        <p><strong>pid:</strong> {proc.pid} · <strong>stamp:</strong> {e(stamp)}</p>
        <p><strong>log:</strong> <a href="{_ROOT}/log-view?path={urllib.parse.quote(log_relpath)}">📜 tail</a></p>
        <p><a href="{_ROOT}/">← back to dashboard</a></p>
        </body></html>"""
    )


# ── Push predictions to dashboard ────────────────────────────────────────────
# Uploads a run's outputs/predictions.csv to the prod dashboard's
# /api/admin/predictions/upload endpoint. Reuses DASHBOARD_URL /
# DASHBOARD_CLIENT_ID / DASHBOARD_CLIENT_SECRET from the caller's env
# (loaded via EnvironmentFile=/home/ubuntu/.env in the systemd unit).
#
# CSV column rename: the acestor pipeline writes camelCase (predictionMin /
# predictionMax); the dashboard's ingest expects snake_case. Rename at the
# boundary rather than in the pipeline so downstream steps (report, downscale,
# rollup) don't need to change their assumptions.
def _detect_scope_id(artifact_group: str) -> str:
    """Guess scope_id from the artifact top-level folder name."""
    g = (artifact_group or "").lower()
    if g == "ka" or g.startswith("ka_"):
        return "karnataka"
    if g.startswith("gba"):
        return "gulb_gba"
    if g == "od" or g.startswith("od_"):
        return "odisha"
    if g.startswith("ap"):
        return "andhra_pradesh"
    return ""


def _monday_of(date_str: str) -> str:
    """Given a YYYY-MM-DD string, return the Monday of that ISO week."""
    from datetime import datetime as _dt, timedelta

    try:
        d = _dt.fromisoformat(date_str).date()
    except (ValueError, TypeError):
        return ""
    return (d - timedelta(days=d.weekday())).isoformat()


_RUN_ID_RE = re.compile(r"^[a-zA-Z0-9_\-.]+$")


def _find_predictions_csv(run_id: str, artifacts_root: Path) -> Path | None:
    """Return the outputs/predictions.csv path for a run_id, or None.

    Rejects run_ids containing path separators, ``..``, or anything else
    outside the safe charset — path composition alone doesn't prevent a
    traversal attempt like ``../../etc/passwd``. Also confirms the resolved
    file actually lives under ``artifacts_root``.
    """
    if not _RUN_ID_RE.match(run_id) or ".." in run_id:
        return None
    for group_dir in artifacts_root.iterdir():
        if not group_dir.is_dir():
            continue
        candidate = (group_dir / run_id / "outputs" / "predictions.csv").resolve()
        # Guard against symlink escapes too.
        try:
            candidate.relative_to(artifacts_root.resolve())
        except ValueError:
            continue
        if candidate.is_file():
            return candidate
    return None


@app.post("/push-predictions")
def push_predictions(
    run_id: str = Form(...),
    scope_id: str = Form(...),
    reference_date: str = Form(...),
    disease: str = Form("Dengue"),
    cfg: Settings = Depends(get_cfg),
):
    """Push a run's predictions.csv to the prod dashboard's ingest endpoint."""
    dash_url = os.environ.get("DASHBOARD_URL", "").rstrip("/")
    client_id = os.environ.get("DASHBOARD_CLIENT_ID", "")
    client_secret = os.environ.get("DASHBOARD_CLIENT_SECRET", "")
    if not (dash_url and client_id and client_secret):
        raise HTTPException(
            500,
            "dashboard credentials missing on the caller — check ~/.env "
            "(DASHBOARD_URL, DASHBOARD_CLIENT_ID, DASHBOARD_CLIENT_SECRET)",
        )

    artifacts_root = cfg.resolved("artifacts_root")
    src = _find_predictions_csv(run_id, artifacts_root)
    if src is None:
        raise HTTPException(
            404,
            f"no predictions.csv found under {artifacts_root} for run_id={run_id}",
        )

    # Rename camelCase → snake_case for min/max at the boundary.
    with (
        open(src) as fin,
        tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as fout,
    ):
        header = fin.readline()
        header = header.replace("predictionMin", "prediction_min").replace(
            "predictionMax", "prediction_max"
        )
        fout.write(header)
        for line in fin:
            fout.write(line)
        renamed_path = fout.name

    # Auth
    try:
        auth_req = urllib.request.Request(
            f"{dash_url}/api/auth/login",
            data=json.dumps({"email": client_id, "password": client_secret}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(auth_req, timeout=15) as resp:
            token = json.loads(resp.read()).get("access_token", "")
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise HTTPException(502, f"dashboard auth failed: {exc}") from None
    if not token:
        raise HTTPException(502, "dashboard returned empty access_token")

    # Upload — use curl subprocess since stdlib multipart is painful
    try:
        out = subprocess.check_output(
            [
                "curl",
                "-sS",
                "-w",
                "\nHTTP %{http_code}",
                "-X",
                "POST",
                f"{dash_url}/api/admin/predictions/upload",
                "-H",
                f"Authorization: Bearer {token}",
                "-F",
                f"file=@{renamed_path}",
                "-F",
                f"scope_id={scope_id}",
                "-F",
                f"reference_date={reference_date}",
                "-F",
                f"disease={disease}",
            ],
            timeout=60,
            stderr=subprocess.STDOUT,
        ).decode()
    finally:
        try:
            Path(renamed_path).unlink()
        except OSError:
            pass

    ok = "HTTP 200" in out
    body, _, status = out.rpartition("\n")
    # Escape every interpolated value — run_id/scope/etc are user-controlled
    # and `body` is dashboard-controlled, both untrusted for HTML rendering.
    e = html.escape
    return HTMLResponse(
        f"""<html><body style="font-family:system-ui;padding:2em">
        <h2>Push to Dashboard · {e(run_id)}</h2>
        <p><strong>scope_id:</strong> {e(scope_id)} · <strong>reference_date:</strong> {e(reference_date)} · <strong>disease:</strong> {e(disease)}</p>
        <p><strong>endpoint:</strong> <code>{e(dash_url)}/api/admin/predictions/upload</code></p>
        <p><strong>{e(status.strip())}</strong> {"✅" if ok else "❌"}</p>
        <pre style="background:#f6f8fa;padding:12px;border-radius:4px;overflow-x:auto">{e(body.strip())}</pre>
        <p><a href="{_ROOT}/run/{urllib.parse.quote(run_id)}">← back to run</a></p>
        </body></html>"""
    )


# ── Settings routes ──────────────────────────────────────────────────────────
@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, cfg: Settings = Depends(get_cfg)):
    return TEMPLATES.TemplateResponse(
        request,
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
    return RedirectResponse(f"{_ROOT}/settings", status_code=303)

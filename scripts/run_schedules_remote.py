"""Remote-run scheduler — DAG-per-state, one trigger cron per state.

Sibling of ``run_schedules.py`` but shells out to ``acestor.remote`` (spawn
EC2 → run pipeline → terminate) instead of ``acestor.run`` (local process).

Meant to live on the caller box (t3.nano / t3.small). Edit the ``STATES``
dict below, then:

    Foreground:   python scripts/run_schedules_remote.py
    Background:   nohup python scripts/run_schedules_remote.py > .acestor/remote-scheduler.out 2>&1 &

DAG semantics
-------------
Each state entry is a *directed acyclic graph* of ``Task`` objects. When
that state's cron fires, we build the graph, launch a thread pool, and:

  * tasks with no ``needs`` start immediately
  * as each task exits ``ok``, any dependent whose full ``needs`` list is
    satisfied becomes eligible and gets submitted to the pool
  * if any task in the DAG fails, its transitive dependents are skipped
    (logged as SKIP, not FAIL — nothing to do about it in this run)
  * independent branches (e.g. ``subdistrict`` and ``ward`` both hanging
    off ``forecast``) run in parallel

A global ``MAX_CONCURRENT`` cap limits how many EC2 instances the caller
has in flight at once (default 4). Cross-state DAG runs share this cap.

Spot lifecycle by default: reclamation mid-run just fails that task, its
downstream is skipped, and the next cron tick tries again. Explicit
checkpoint/resume is not implemented — deferred until it hurts.

Dashboard credentials come from ``~/.env`` via python-dotenv. Set:

    DASHBOARD_URL=https://apps.artpark.ai/disease-dashboard-staging
    DASHBOARD_CLIENT_ID=admin@dengue.local
    DASHBOARD_CLIENT_SECRET=...
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, Future
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = ROOT / "logs"

load_dotenv(Path.home() / ".env")

# ── Static remote runner config ────────────────────────────────────────────────
AWS_KEY_NAME = os.environ.get("ACESTOR_AWS_KEY_NAME", "acestor-smoke-202607311430")
AWS_KEY_PATH = os.path.expanduser(
    os.environ.get("ACESTOR_AWS_KEY_PATH", "~/.ssh/acestor-smoke.pem")
)
# Optional custom AMI (tier-2: ubuntu + python + uv + build-deps pre-installed
# → skips ~30-60s of apt-install per spawn). If unset, acestor.remote falls
# back to the latest Ubuntu 24.04 AMI via SSM — works on any AWS account with
# no prior bake step, just slower per run.
AWS_AMI = os.environ.get("ACESTOR_AWS_AMI") or None
FORWARD_ENV = (
    "DASHBOARD_URL",
    "DASHBOARD_CLIENT_ID",
    "DASHBOARD_CLIENT_SECRET",
    "DASHBOARD_SELECTED_REGION_ID",
)

# Global cap on concurrent EC2 spawns across ALL state DAGs. Bump if you
# have more per-region vCPU / instance quota headroom.
MAX_CONCURRENT = int(os.environ.get("ACESTOR_MAX_CONCURRENT", "4"))

# Hard timeout per task (minutes) — subprocess.run raises TimeoutExpired.
# 45 min covers the slowest observed forecast comfortably; anything longer
# is almost certainly hung. On timeout the subprocess is killed → its
# compute EC2 will be reaped by scripts/reap_orphan_compute.sh on the next
# cron tick.
MAX_TASK_MINUTES = int(os.environ.get("ACESTOR_MAX_TASK_MINUTES", "45"))

# Retry a task up to this many times if the subprocess was killed by a
# signal (SIGTERM/SIGKILL) or hit MAX_TASK_MINUTES. Real pipeline errors
# (exit 1 etc.) are NOT retried — they'd just fail again wasting compute.
MAX_ATTEMPTS = int(os.environ.get("ACESTOR_MAX_ATTEMPTS", "2"))
RETRY_BACKOFF_SEC = int(os.environ.get("ACESTOR_RETRY_BACKOFF_SEC", "30"))

# Append-only JSONL log of every task attempt. Serves two purposes:
#   1. Post-hoc audit: which tasks retried, why, when
#   2. On future scheduler restart, could be replayed for cross-restart
#      retry state — not implemented today since the DAG runner itself
#      doesn't survive restarts, so the retry-state bookkeeping alone
#      would be dangling.
ATTEMPTS_LOG_PATH = Path.home() / ".acestor" / "scheduler_attempts.jsonl"
_attempts_lock = threading.Lock()


@dataclass
class Task:
    """One node in a state's DAG.

    ``needs`` is a list of sibling ``name``s that must all reach ``ok``
    before this task becomes eligible.

    ``source_of`` (optional) names the sibling task whose run_id should
    be inherited as ``run.source_run_id`` for this task. Used by
    downscale/rollup tasks to point at the parent forecast/downscale
    they consume. When set, the scheduler:
      1. Passes ``--set run.source_run_id=<sibling_run_id>`` so this
         task reads a specific run (not the "latest" sentinel that
         relies on artifact-directory scanning).
      2. Passes ``--extra-input-path <source_artifacts_root>/<sibling_run_id>``
         so ONLY that run's artifact subtree is rsync'd to the compute
         box — not the entire artifacts/ tree. ``source_artifacts_root``
         is read from the source task's own
         ``storages.artifacts.filesystem.base_path`` (falls back to
         ``artifacts/<state>`` when absent).
    """

    name: str
    pipeline: str
    config: str
    instance: str
    lifecycle: str = "spot"  # or "on-demand"
    needs: list[str] = field(default_factory=list)
    source_of: str | None = None
    # Auto-push the task's predictions.csv to the prod dashboard once
    # the task finishes OK. When set, the scheduler synthesises a sibling
    # "<name>-push" task that runs locally on the caller (no EC2 spawn)
    # and depends on this task. The push task fails if the dashboard
    # upload fails, which blocks any downstream task that depends on it.
    # Opt-in per task. Not applied to UI-triggered runs.
    # Shape: {"scope_id": "karnataka"}
    auto_push: dict | None = None


_PREP = "pipelines.dengue_prep.pipeline:build_pipeline"
_FORECAST = "pipelines.dengue.pipeline:build_pipeline"
_DOWNSCALE = "pipelines.dengue_downscale.pipeline:build_pipeline"
_ROLLUP = "pipelines.dengue_rollup.pipeline:build_pipeline"
# Pseudo-pipeline marker for synthesised push tasks (see Task.auto_push).
# Tasks with pipeline == _PUSH run locally on the caller via
# _execute_push_locally instead of acestor.remote.
_PUSH = "local:push_predictions"

# ── Per-state DAGs — edit below to add / remove pipelines ─────────────────────
STATES: dict[str, dict] = {
    "ka": {
        # APScheduler's CronTrigger.from_crontab is off-by-one on the DOW
        # integer field (treats '1' as Tuesday, not Monday). Use the named
        # literal 'mon' which it handles correctly. Last three runs that
        # should have been Monday fired on Tuesday because of this.
        "cron": "0 2 * * mon",  # 02:00 UTC every Monday
        "dag": [
            # t3.small (not t3.micro) — the fetch step accumulates one full
            # chunk_days-sized window as a Python records list before yielding
            # a DataFrame. For KA at scale (~134k rows in a peak year) this
            # exceeds 1 GB peak RAM on t3.micro during full-history seeds.
            # See _download_xlsx streaming plan for the follow-up.
            Task("district-prep", _PREP, "configs/ka_district_prep.yaml", "t3.small"),
            Task(
                "subdistrict-prep",
                _PREP,
                "configs/ka_subdistrict_prep.yaml",
                "t3.small",
            ),
            Task(
                "forecast",
                _FORECAST,
                "configs/ka_district.yaml",
                "c7i.4xlarge",
                needs=["district-prep"],
                auto_push={"scope_id": "karnataka"},
            ),
            Task(
                "subdistrict-downscale",
                _DOWNSCALE,
                "configs/ka_district_to_subdistrict.yaml",
                "t3.micro",
                needs=["forecast", "subdistrict-prep"],
                source_of="forecast",
                auto_push={"scope_id": "karnataka"},
            ),
        ],
    },
    "gba": {
        "cron": "30 2 * * mon",  # 02:30 UTC every Monday
        # Zone is the source-of-truth level for GBA:
        # * prep zone/corp/ward in parallel
        # * forecast at zone
        # * rollup zone→corp AND downscale zone→ward in parallel
        "dag": [
            Task("zone-prep", _PREP, "configs/gba_zone_prep.yaml", "t3.micro"),
            Task("corp-prep", _PREP, "configs/gba_corp_prep.yaml", "t3.micro"),
            Task("ward-prep", _PREP, "configs/gba_ward_prep.yaml", "t3.micro"),
            Task(
                "zone-forecast",
                _FORECAST,
                "configs/gba_zone.yaml",
                "c7i.4xlarge",
                needs=["zone-prep"],
                auto_push={"scope_id": "gulb_gba"},
            ),
            Task(
                "corp-rollup",
                _ROLLUP,
                "configs/gba_zone_to_corp.yaml",
                "t3.nano",
                needs=["zone-forecast", "corp-prep"],
                source_of="zone-forecast",
                auto_push={"scope_id": "gulb_gba"},
            ),
            Task(
                "ward-downscale",
                _DOWNSCALE,
                "configs/gba_zone_to_ward.yaml",
                "t3.micro",
                needs=["zone-forecast", "ward-prep"],
                source_of="zone-forecast",
                auto_push={"scope_id": "gulb_gba"},
            ),
        ],
    },
    "od": {
        "cron": "0 3 * * mon",  # 03:00 UTC every Monday
        # District is source-of-truth; block + ulb are parallel downscales,
        # ulb_ward hangs off ulb.
        "dag": [
            Task("district-prep", _PREP, "configs/od_district_prep.yaml", "t3.micro"),
            Task("block-prep", _PREP, "configs/od_block_prep.yaml", "t3.micro"),
            Task("ulb-prep", _PREP, "configs/od_ulb_prep.yaml", "t3.micro"),
            Task(
                "ulb-ward-prep",
                _PREP,
                "configs/od_ulb_ward_prep.yaml",
                "t3.micro",
            ),
            Task(
                "district-forecast",
                _FORECAST,
                "configs/od_district.yaml",
                "c7i.4xlarge",
                needs=["district-prep"],
                auto_push={"scope_id": "odisha"},
            ),
            Task(
                "block-downscale",
                _DOWNSCALE,
                "configs/od_district_to_block.yaml",
                "t3.micro",
                needs=["district-forecast", "block-prep"],
                source_of="district-forecast",
                auto_push={"scope_id": "odisha"},
            ),
            Task(
                "ulb-downscale",
                _DOWNSCALE,
                "configs/od_district_to_ulb.yaml",
                "t3.micro",
                needs=["district-forecast", "ulb-prep"],
                source_of="district-forecast",
                auto_push={"scope_id": "odisha"},
            ),
            Task(
                "ulb-ward-downscale",
                _DOWNSCALE,
                "configs/od_ulb_to_ward.yaml",
                "t3.micro",
                needs=["ulb-downscale", "ulb-ward-prep"],
                source_of="ulb-downscale",
                auto_push={"scope_id": "odisha"},
            ),
        ],
    },
}
# ──────────────────────────────────────────────────────────────────────────────


# Global executor shared across all state DAG runs → cross-state parallelism
# naturally caps at MAX_CONCURRENT. Threads are fine here because every task
# just supervises a subprocess (network-bound), no CPU-bound work in-process.
_pool = ThreadPoolExecutor(max_workers=MAX_CONCURRENT, thread_name_prefix="acestor-dag")


def _print(msg: str) -> None:
    print(f"[{datetime.utcnow().isoformat()}Z] scheduler: {msg}", flush=True)


def _build_remote_cmd(state: str, task: Task, run_id: str, run_stamp: str) -> list[str]:
    cmd = [
        sys.executable,
        "-m",
        "acestor.remote",
        "--pipeline",
        task.pipeline,
        "--config",
        task.config,
        "--run-id",
        run_id,
        "--remote-instance",
        task.instance,
        "--remote-lifecycle",
        task.lifecycle,
        "--aws-key-name",
        AWS_KEY_NAME,
        "--aws-key-path",
        AWS_KEY_PATH,
    ]
    if AWS_AMI:
        cmd.extend(["--aws-ami", AWS_AMI])
    for env_name in FORWARD_ENV:
        cmd.extend(["--forward-env", env_name])

    # source_of wiring: this task consumes another task's output.
    # Compute the source's run_id and pass both:
    #   1. --set run.source_run_id=<...>  → config override so the pipeline
    #      reads a specific run instead of scanning for "latest"
    #   2. --extra-input-path <source_artifacts_root>/<source_run_id> → push
    #      only that one run's artifact subtree to the compute box. The root
    #      is resolved from the SOURCE task's own config
    #      (storages.artifacts.filesystem.base_path) so states that use a
    #      per-level artifact dir like `artifacts/gba_zone/` work — the naive
    #      `artifacts/<state>/` assumption was wrong for those.
    if task.source_of:
        source_run_id = f"{state}-{task.source_of}_{run_stamp}"
        cmd.extend(["--set", f"run.source_run_id={source_run_id}"])
        source_artifacts_root = _resolve_source_artifacts_root(state, task.source_of)
        cmd.extend(["--extra-input-path", f"{source_artifacts_root}/{source_run_id}"])
    return cmd


def _resolve_source_artifacts_root(state: str, source_task_name: str) -> str:
    """Return the artifacts root path where the source task writes its outputs.

    Reads ``storages.artifacts.filesystem.base_path`` from the source task's
    config. Falls back to the legacy ``artifacts/<state>`` convention if the
    key isn't set or the config can't be parsed — matches historical
    behaviour for OD/KA which happen to follow that layout.
    """
    fallback = f"artifacts/{state}"
    source_task = next(
        (t for t in STATES[state]["dag"] if t.name == source_task_name), None
    )
    if source_task is None:
        return fallback
    cfg_path = Path(source_task.config)
    if not cfg_path.exists():
        return fallback
    try:
        cfg = yaml.safe_load(cfg_path.read_text()) or {}
    except yaml.YAMLError:
        return fallback
    base = (
        (cfg.get("storages") or {})
        .get("artifacts", {})
        .get("filesystem", {})
        .get("base_path")
    )
    return base or fallback


def _record_attempt(
    state: str,
    dag_run_id: str,
    task: str,
    attempt: int,
    exit_status: str,
    exit_code: int,
    wall_seconds: float,
) -> None:
    """Append one JSON line to the scheduler attempts log."""
    row = {
        "ts": datetime.utcnow().isoformat() + "Z",
        "state": state,
        "dag_run_id": dag_run_id,
        "task": task,
        "attempt": attempt,
        "exit_status": exit_status,  # "ok" | "fail" | "signal" | "timeout"
        "exit_code": exit_code,
        "wall_seconds": round(wall_seconds, 1),
    }
    line = json.dumps(row, separators=(",", ":"))
    with _attempts_lock:
        ATTEMPTS_LOG_PATH.parent.mkdir(exist_ok=True)
        with open(ATTEMPTS_LOG_PATH, "a") as f:
            f.write(line + "\n")


def _classify_exit(exit_code: int, timed_out: bool) -> str:
    """Turn a subprocess return code into a coarse category for the ledger
    + retry decision.

    Real pipeline error → 'fail' (exit 1 etc., don't retry).
    Killed by signal / timeout → 'signal' or 'timeout' (retry).
    """
    if timed_out:
        return "timeout"
    if exit_code == 0:
        return "ok"
    # Python subprocess: negative returncode = killed by signal (unhandled).
    # Positive 128+N convention = shell reported the signal.
    if exit_code < 0 or exit_code in (130, 137, 143):  # SIGINT/SIGKILL/SIGTERM
        return "signal"
    return "fail"


def _expand_dag_with_pushes(dag: list[Task]) -> list[Task]:
    """Insert a synthetic '<name>-push' task after every task with
    ``auto_push`` set. The push task runs locally on the caller (no EC2
    spawn), depends on the parent task completing OK, and uploads the
    parent's ``predictions.csv`` to the prod dashboard.

    Called once per DAG trigger. Returns a new list; does not mutate.
    """
    out: list[Task] = []
    for t in dag:
        out.append(t)
        if t.auto_push:
            scope_id = t.auto_push.get("scope_id", "")
            if not scope_id:
                raise ValueError(
                    f"{t.name}: auto_push requires 'scope_id', got {t.auto_push!r}"
                )
            out.append(
                Task(
                    name=f"{t.name}-push",
                    pipeline=_PUSH,
                    config="",  # unused for local push
                    instance="local",
                    needs=[t.name],
                    # Carry the push target forward via auto_push so the
                    # executor can read it. The push task itself has no
                    # further auto_push (no recursion).
                    auto_push={"scope_id": scope_id, "parent": t.name},
                )
            )
    return out


def _execute_push_locally(
    state: str, task: Task, run_stamp: str, attempt: int
) -> tuple[int, str, float]:
    """Push the parent task's predictions.csv to the dashboard.

    Runs in the scheduler's thread pool on the caller — no EC2 spawn, no
    SSH. Finds predictions.csv under
    ``artifacts/<state>/<parent_run_id>/outputs/predictions.csv``, renames
    camelCase min/max columns to snake_case, POSTs multipart to
    ``$DASHBOARD_URL/api/admin/predictions/upload``. Exit 0 on 2xx, 1 on
    anything else. Failure here fails the push task, which blocks any
    downstream task that depends on it.
    """
    suffix = "" if attempt == 1 else f"_r{attempt - 1}"
    run_id = f"{state}-{task.name}_{run_stamp}{suffix}"
    log_path = LOGS_DIR / f"{state}-{task.name}" / run_stamp[:10] / f"{run_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    tag = f"{state}/{task.name}" + (f" [attempt {attempt}]" if attempt > 1 else "")
    started = time.monotonic()

    parent_name = (task.auto_push or {}).get("parent", "")
    scope_id = (task.auto_push or {}).get("scope_id", "")
    # Parent task's run_id has the same stamp (same DAG tick) — no retry
    # suffix because the push only fires once the parent is OK.
    parent_run_id = f"{state}-{parent_name}_{run_stamp}"
    # Reference date for the upload is the stamp's date portion.
    reference_date = run_stamp[:10]

    with open(log_path, "w") as log:

        def _log(msg: str) -> None:
            line = f"[{datetime.utcnow().isoformat()}Z] push: {msg}\n"
            log.write(line)
            log.flush()

        _log(f"start tag={tag} parent={parent_run_id} scope_id={scope_id}")
        try:
            exit_code = _do_push(
                state=state,
                parent_run_id=parent_run_id,
                scope_id=scope_id,
                reference_date=reference_date,
                log=_log,
            )
        except Exception as exc:
            _log(f"FAILED unexpected: {type(exc).__name__}: {exc}")
            exit_code = 1

    wall_s = time.monotonic() - started
    classification = "ok" if exit_code == 0 else "fail"
    _record_attempt(
        state, run_stamp, task.name, attempt, classification, exit_code, wall_s
    )
    _print(
        f"{'OK' if classification == 'ok' else 'FAIL'}    {tag}: push {classification} "
        f"in {wall_s:.1f}s → {log_path}"
    )
    return exit_code, classification, wall_s


def _do_push(
    state: str,
    parent_run_id: str,
    scope_id: str,
    reference_date: str,
    log,
) -> int:
    """Core push HTTP flow. Returns 0 on success, 1 on any failure."""
    import tempfile
    import urllib.error
    import urllib.request

    dash_url = os.environ.get("DASHBOARD_URL", "").rstrip("/")
    client_id = os.environ.get("DASHBOARD_CLIENT_ID", "")
    client_secret = os.environ.get("DASHBOARD_CLIENT_SECRET", "")
    if not (dash_url and client_id and client_secret):
        log(
            "missing DASHBOARD_URL / DASHBOARD_CLIENT_ID / DASHBOARD_CLIENT_SECRET on caller"
        )
        return 1

    # Resolve predictions.csv path. The artifacts root for the parent task
    # is under artifacts/<state>/<parent_run_id>/outputs/predictions.csv.
    # Some pipelines use <state>_<region_type> (e.g. gba_zone) as the group;
    # try the obvious first then fall back to a glob.
    candidates = list(ROOT.glob(f"artifacts/*/{parent_run_id}/outputs/predictions.csv"))
    if not candidates:
        log(f"no predictions.csv found for parent run_id={parent_run_id}")
        return 1
    src = candidates[0]
    log(f"found predictions.csv at {src}")

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
        with urllib.request.urlopen(auth_req, timeout=20) as resp:
            token = json.loads(resp.read()).get("access_token", "")
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        log(f"auth failed: {exc}")
        return 1
    if not token:
        log("auth returned empty access_token")
        return 1
    log("auth OK, uploading…")

    # Upload — curl handles multipart cleanly
    try:
        out = subprocess.run(
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
                "disease=Dengue",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        log("upload curl timed out after 120s")
        return 1
    body = out.stdout + ("\n" + out.stderr if out.stderr else "")
    log(f"upload response:\n{body.rstrip()}")
    # curl -w prints the HTTP code on its own line at the end
    last_line = (out.stdout.strip().splitlines() or [""])[-1]
    if last_line.startswith("HTTP ") and last_line.split()[1].startswith("2"):
        log(f"upload OK ({last_line})")
        return 0
    log(f"upload FAILED ({last_line or 'no response'})")
    return 1


def _execute_task(
    state: str, task: Task, run_stamp: str, attempt: int
) -> tuple[int, str, float]:
    """Shell out to ``acestor.remote`` once; return (exit_code, classification, wall_s).

    Enforces a hard MAX_TASK_MINUTES timeout via subprocess.run — anything
    slower than that is presumed hung, subprocess is killed, and the compute
    EC2 will be reaped by reap_orphan_compute.sh on its cron cycle.

    Local tasks (pipeline == _PUSH) are dispatched to _execute_push_locally
    which runs on the caller with no EC2 spawn.
    """
    if task.pipeline == _PUSH:
        return _execute_push_locally(state, task, run_stamp, attempt)
    suffix = "" if attempt == 1 else f"_r{attempt - 1}"
    run_id = f"{state}-{task.name}_{run_stamp}{suffix}"
    log_path = LOGS_DIR / f"{state}-{task.name}" / run_stamp[:10] / f"{run_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    tag = f"{state}/{task.name}" + (f" [attempt {attempt}]" if attempt > 1 else "")
    _print(f"START  {tag} → {log_path}")
    started = time.monotonic()
    timed_out = False
    try:
        with open(log_path, "w") as f:
            result = subprocess.run(
                _build_remote_cmd(state, task, run_id, run_stamp),
                stdout=f,
                stderr=f,
                cwd=str(ROOT),
                timeout=MAX_TASK_MINUTES * 60,
            )
        exit_code = result.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        exit_code = -1
        _print(
            f"TIMEOUT {tag}: exceeded {MAX_TASK_MINUTES}m wall — "
            f"subprocess killed; reaper will terminate the compute EC2"
        )
    wall_s = time.monotonic() - started
    classification = _classify_exit(exit_code, timed_out)
    _record_attempt(
        state, run_stamp, task.name, attempt, classification, exit_code, wall_s
    )
    return exit_code, classification, wall_s


def _run_dag(state: str, dag: list[Task]) -> None:
    """Traverse ``dag`` respecting deps + concurrency cap.

    A task is submitted the moment all its ``needs`` have completed OK.
    Failed / skipped tasks poison their transitive dependents (skipped,
    not retried within this cron tick).

    Any task with ``auto_push`` set is followed by a synthesised local
    push task that uploads its predictions.csv to the prod dashboard.
    See _expand_dag_with_pushes.
    """
    dag = _expand_dag_with_pushes(dag)
    _validate_dag(state, dag)

    status: dict[str, str] = {}  # name → "ok" | "fail" | "skip"
    attempts: dict[str, int] = {}  # name → attempts fired so far
    futures: dict[str, Future] = {}
    completed = threading.Event()
    lock = threading.Lock()  # protects status/attempts/futures across callbacks

    run_stamp = datetime.utcnow().strftime("%Y-%m-%d_%H%M%S")
    _print(f"DAG    {state}: starting run @ {run_stamp} — {len(dag)} task(s)")

    def _submit_task(t: Task) -> None:
        """Increment attempt counter + submit to the pool."""
        attempts[t.name] = attempts.get(t.name, 0) + 1
        fut = _pool.submit(_execute_task, state, t, run_stamp, attempts[t.name])
        futures[t.name] = fut
        fut.add_done_callback(lambda f, name=t.name: _on_done(name, f))

    def _submit_ready() -> None:
        """Submit any task whose deps are all done + OK, not yet running."""
        with lock:
            for t in dag:
                if t.name in status or t.name in futures:
                    continue  # already handled or in flight
                deps_status = [status.get(dep) for dep in t.needs]
                if any(s is None for s in deps_status):
                    continue  # some dep still in flight
                if any(s != "ok" for s in deps_status):
                    status[t.name] = "skip"
                    _print(
                        f"SKIP   {state}/{t.name}: upstream failed "
                        f"({[(d, status.get(d)) for d in t.needs]})"
                    )
                    continue
                _submit_task(t)

    def _retry_after_backoff(t: Task) -> None:
        time.sleep(RETRY_BACKOFF_SEC)
        with lock:
            futures.pop(t.name, None)
            _submit_task(t)

    def _on_done(name: str, fut: Future) -> None:
        task_obj = next(t for t in dag if t.name == name)
        try:
            exit_code, classification, _ = fut.result()
        except Exception as exc:
            classification = "fail"
            exit_code = -1
            _print(f"FAIL  {state}/{name}: raised {type(exc).__name__}: {exc}")

        with lock:
            futures.pop(name, None)

        if classification == "ok":
            with lock:
                status[name] = "ok"
            _print(f"OK    {state}/{name}: acestor.remote exit=0")
        elif (
            classification in ("signal", "timeout")
            and attempts[name] < MAX_ATTEMPTS + 1
        ):
            # Retry-eligible: killed by signal (external or self-timeout)
            # rather than a real pipeline error. Spin a helper thread to
            # sleep the backoff then re-submit (can't sleep in the pool
            # callback — that would burn a worker slot).
            _print(
                f"RETRY {state}/{name}: classification={classification} "
                f"exit={exit_code} → sleeping {RETRY_BACKOFF_SEC}s then "
                f"attempt {attempts[name] + 1}/{MAX_ATTEMPTS + 1}"
            )
            threading.Thread(
                target=_retry_after_backoff, args=(task_obj,), daemon=True
            ).start()
            return  # don't count as terminal
        else:
            with lock:
                status[name] = "fail"
            _print(
                f"FAIL  {state}/{name}: classification={classification} "
                f"exit={exit_code} (attempts={attempts[name]})"
            )

        _submit_ready()
        with lock:
            if len(status) == len(dag):
                completed.set()

    _submit_ready()
    completed.wait()

    ok = sum(1 for s in status.values() if s == "ok")
    fail = sum(1 for s in status.values() if s == "fail")
    skip = sum(1 for s in status.values() if s == "skip")
    _print(f"DAG    {state}: done — ok={ok} fail={fail} skip={skip} " f"of {len(dag)}")


def _validate_dag(state: str, dag: list[Task]) -> None:
    names = {t.name for t in dag}
    for t in dag:
        for dep in t.needs:
            if dep not in names:
                raise ValueError(
                    f"{state}: task {t.name!r} needs {dep!r} which is not defined."
                )
    # cycle check: repeated Kahn topological sort
    remaining = {t.name: set(t.needs) for t in dag}
    while remaining:
        free = [n for n, deps in remaining.items() if not deps]
        if not free:
            raise ValueError(f"{state}: DAG has a cycle involving {sorted(remaining)}")
        for n in free:
            remaining.pop(n)
            for deps in remaining.values():
                deps.discard(n)


def _make_dag_trigger(state: str, dag: list[Task]):
    def trigger() -> None:
        try:
            _run_dag(state, dag)
        except Exception as exc:  # pragma: no cover
            _print(f"FATAL  {state}: DAG runner crashed — {type(exc).__name__}: {exc}")

    return trigger


def main() -> None:
    LOGS_DIR.mkdir(exist_ok=True)
    if not STATES:
        raise SystemExit("STATES is empty — nothing to schedule.")

    # Fail fast on bad DAG shapes before starting the scheduler loop.
    for state, spec in STATES.items():
        _validate_dag(state, spec["dag"])

    # BackgroundScheduler because our trigger callables block on the DAG run
    # (which itself blocks on the thread pool). A BlockingScheduler + long
    # trigger callable would starve other state crons. Background + our own
    # ThreadPoolExecutor keeps everything responsive.
    sched = BackgroundScheduler(timezone="UTC")

    for state, spec in STATES.items():
        sched.add_job(
            _make_dag_trigger(state, spec["dag"]),
            CronTrigger.from_crontab(spec["cron"], timezone="UTC"),
            id=f"dag-{state}",
            name=f"dag-{state}",
            misfire_grace_time=3600,
            max_instances=1,  # if a DAG run overruns, skip the next tick
        )
        task_names = [t.name for t in spec["dag"]]
        print(f"Scheduled: {state}  ({spec['cron']} UTC) → {task_names}")

    print(
        f"Remote scheduler running (max_concurrent={MAX_CONCURRENT}). "
        f"Ctrl+C to stop."
    )
    sched.start()
    try:
        threading.Event().wait()  # sleep forever; APScheduler runs in the background
    except (KeyboardInterrupt, SystemExit):
        print("Stopping scheduler…")
        sched.shutdown(wait=True)
        _pool.shutdown(wait=True, cancel_futures=True)
        print("Stopped.")


if __name__ == "__main__":
    main()

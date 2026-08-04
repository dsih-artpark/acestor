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
    """

    name: str
    pipeline: str
    config: str
    instance: str
    lifecycle: str = "spot"  # or "on-demand"
    needs: list[str] = field(default_factory=list)


_PREP = "pipelines.dengue_prep.pipeline:build_pipeline"
_FORECAST = "pipelines.dengue.pipeline:build_pipeline"
_DOWNSCALE = "pipelines.dengue_downscale.pipeline:build_pipeline"
_ROLLUP = "pipelines.dengue_rollup.pipeline:build_pipeline"

# ── Per-state DAGs — edit below to add / remove pipelines ─────────────────────
STATES: dict[str, dict] = {
    "ka": {
        "cron": "0 * * * *",  # TEMP: every hour for testing (was: 02:00 UTC daily)
        "dag": [
            Task("prep", _PREP, "configs/ka_district_prep.yaml", "t3.large"),
            Task(
                "subdistrict-prep",
                _PREP,
                "configs/ka_subdistrict_prep.yaml",
                "t3.large",
            ),
            Task(
                "forecast",
                _FORECAST,
                "configs/ka_district.yaml",
                "c7i.4xlarge",
                needs=["prep"],
            ),
            Task(
                "subdistrict-downscale",
                _DOWNSCALE,
                "configs/ka_district_to_subdistrict.yaml",
                "c7i.2xlarge",
                needs=["forecast", "subdistrict-prep"],
            ),
        ],
    },
    "gba": {
        "cron": "0 * * * *",  # TEMP: every hour for testing (was: 02:30 UTC daily)
        # Zone is the source-of-truth level for GBA:
        # * prep zone/corp/ward in parallel
        # * forecast at zone
        # * rollup zone→corp AND downscale zone→ward in parallel
        "dag": [
            Task("zone-prep", _PREP, "configs/gba_zone_prep.yaml", "t3.large"),
            Task("corp-prep", _PREP, "configs/gba_corp_prep.yaml", "t3.medium"),
            Task("ward-prep", _PREP, "configs/gba_ward_prep.yaml", "t3.medium"),
            Task(
                "zone-forecast",
                _FORECAST,
                "configs/gba_zone.yaml",
                "c7i.4xlarge",
                needs=["zone-prep"],
            ),
            Task(
                "corp-rollup",
                _ROLLUP,
                "configs/gba_zone_to_corp.yaml",
                "c7i.2xlarge",
                needs=["zone-forecast", "corp-prep"],
            ),
            Task(
                "ward-downscale",
                _DOWNSCALE,
                "configs/gba_zone_to_ward.yaml",
                "c7i.2xlarge",
                needs=["zone-forecast", "ward-prep"],
            ),
        ],
    },
    "od": {
        "cron": "0 * * * *",  # TEMP: every hour for testing (was: 03:00 UTC daily)
        # District is source-of-truth; block + ulb are parallel downscales,
        # ulb_ward hangs off ulb.
        "dag": [
            Task("district-prep", _PREP, "configs/od_district_prep.yaml", "t3.large"),
            Task("block-prep", _PREP, "configs/od_block_prep.yaml", "t3.medium"),
            Task("ulb-prep", _PREP, "configs/od_ulb_prep.yaml", "t3.medium"),
            Task(
                "ulb-ward-prep",
                _PREP,
                "configs/od_ulb_ward_prep.yaml",
                "t3.medium",
            ),
            Task(
                "district-forecast",
                _FORECAST,
                "configs/od_district.yaml",
                "c7i.4xlarge",
                needs=["district-prep"],
            ),
            Task(
                "block-downscale",
                _DOWNSCALE,
                "configs/od_district_to_block.yaml",
                "c7i.2xlarge",
                needs=["district-forecast", "block-prep"],
            ),
            Task(
                "ulb-downscale",
                _DOWNSCALE,
                "configs/od_district_to_ulb.yaml",
                "c7i.2xlarge",
                needs=["district-forecast", "ulb-prep"],
            ),
            Task(
                "ulb-ward-downscale",
                _DOWNSCALE,
                "configs/od_ulb_to_ward.yaml",
                "c7i.2xlarge",
                needs=["ulb-downscale", "ulb-ward-prep"],
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


def _build_remote_cmd(state: str, task: Task, run_id: str) -> list[str]:
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
    return cmd


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


def _execute_task(
    state: str, task: Task, run_stamp: str, attempt: int
) -> tuple[int, str, float]:
    """Shell out to ``acestor.remote`` once; return (exit_code, classification, wall_s).

    Enforces a hard MAX_TASK_MINUTES timeout via subprocess.run — anything
    slower than that is presumed hung, subprocess is killed, and the compute
    EC2 will be reaped by reap_orphan_compute.sh on its cron cycle.
    """
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
                _build_remote_cmd(state, task, run_id),
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
    """
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

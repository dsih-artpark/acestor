"""Orchestrates the 6-step remote-run lifecycle.

    provision → bootstrap → sync-up → run → sync-down → terminate

Slice 1 wires provision + terminate + ledger only. Bootstrap / sync / SSH
exec land in later slices — each one drops in behind the same
:class:`RemoteRunOptions` surface so the CLI never grows a new flag per
slice.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from pathlib import Path

from acestor.remote.bootstrap import (
    remote_run_command,
    system_bootstrap_script,
    uv_sync_command,
)
from acestor.remote.config_walker import (
    find_input_paths,
    find_output_paths,
    find_output_paths_scoped_to_region,
)
from acestor.remote.env_forward import DEFAULT_FORWARD_ENV, compose_env_prefix
from acestor.remote.ledger import (
    DEFAULT_LEDGER_PATH,
    ExitStatus,
    RemoteRunRecord,
    append_run_record,
    utc_now_iso,
)
from acestor.remote.providers.base import CloudProvider, Lifecycle, RemoteHost
from acestor.remote.ssh import ssh_exec
from acestor.remote.sync import (
    sync_artifacts_back,
    sync_hyperparams_up,
    sync_input_datasets,
    sync_output_datasets_back,
    sync_repo,
)

log = logging.getLogger(__name__)


@dataclass
class RemoteRunOptions:
    pipeline: str
    config: str
    run_id: str
    provider: CloudProvider
    instance_type: str
    lifecycle: Lifecycle
    region: str
    overrides: list[str] = field(default_factory=list)
    clean: bool = False
    ledger_path: Any = (
        DEFAULT_LEDGER_PATH  # Path — Any keeps the dataclass import clean
    )
    # Slice 3+ knobs
    repo_root: Any = None  # Path — resolved to project root in run_remote if None
    artifacts_local_root: Any = None  # Path — defaults to <repo_root>/artifacts
    uv_extras: str = "all"  # forwarded to bootstrap.uv_sync_command
    skip_run: bool = False  # bootstrap + sync only, don't execute the pipeline
    keep_alive_on_failure: bool = False  # leave host up for debugging (charges!)
    forward_env: tuple[str, ...] = DEFAULT_FORWARD_ENV  # env vars to forward
    skip_input_sync: bool = False  # skip config-walked dataset rsync
    # Extra local dirs to push up in addition to config-walked inputs.
    # Used for per-run artifact subtrees (e.g. artifacts/od/od-district-
    # forecast_.../ needed by a downscale) — pushes only that specific
    # directory instead of the whole artifacts/ tree.
    extra_input_paths: tuple[str, ...] = ()


@dataclass
class RemoteRunOutcome:
    exit_status: ExitStatus
    wall_seconds: float
    host: RemoteHost | None
    error: str = ""
    artifact_bytes: int = 0


def run_remote(opts: RemoteRunOptions) -> RemoteRunOutcome:
    """Execute the pipeline on a freshly provisioned remote host.

    Guarantees ``terminate`` is called and one ledger row is appended, no
    matter how the run exits. This is the sole entry point for the CLI.
    """
    started = time.monotonic()
    started_iso = utc_now_iso()

    host: RemoteHost | None = None
    exit_status: ExitStatus = "unknown"
    error = ""
    artifact_bytes = 0

    repo_root = Path(opts.repo_root) if opts.repo_root else _find_repo_root()
    local_artifacts_root = (
        Path(opts.artifacts_local_root)
        if opts.artifacts_local_root
        else repo_root / "artifacts"
    )

    try:
        # ── 1. Provision ────────────────────────────────────────────────
        log.info(
            "remote runner: provisioning %s (%s) via %s in %s for run_id=%s",
            opts.instance_type,
            opts.lifecycle,
            opts.provider.name,
            opts.region,
            opts.run_id,
        )
        host = opts.provider.provision(
            instance_type=opts.instance_type,
            lifecycle=opts.lifecycle,
            region=opts.region,
            run_id=opts.run_id,
        )
        log.info(
            "remote runner: host ready — id=%s ip=%s user=%s",
            host.id,
            host.ip,
            host.ssh_user,
        )

        # Mock provider points at localhost with no key — skip the SSH/sync
        # phases so the wiring test in slice 1 still passes.
        if not host.ssh_key_path or host.ip == "127.0.0.1":
            log.info("remote runner: mock host — skipping bootstrap/sync/exec phases")
            exit_status = "ok"
        else:
            # ── 2. Repo sync up ─────────────────────────────────────────
            try:
                sync_repo(host, repo_root)
            except Exception as exc:
                exit_status = "sync_failed"
                error = f"repo sync failed: {exc}"
                raise

            # ── 2b. Input + output dataset sync (skips gracefully when
            #        configs use dashboard sources → no local dirs exist).
            #        Output paths are pushed too so the pipeline sees the
            #        existing cache (e.g. main dengue pipeline reads
            #        prepared_data as input); they'll be pulled back later. ──
            if not opts.skip_input_sync:
                try:
                    inputs = _load_input_paths(opts.config, repo_root)
                    outputs = _load_output_paths(opts.config, repo_root)
                    # Outputs that already exist locally count as inputs too
                    # for the push-up phase (bidirectional dirs).
                    push_up = list(inputs) + [
                        op for op in outputs if op.local_path.exists()
                    ]
                    # --extra-input-path: caller passes additional dirs to
                    # rsync-up (typically per-run artifact subtrees a
                    # downscale/rollup needs). Resolve, validate, dedup, add.
                    from acestor.remote.config_walker import InputPath

                    for raw in opts.extra_input_paths:
                        p = Path(raw)
                        if not p.is_absolute():
                            p = (repo_root / p).resolve()
                        else:
                            p = p.resolve()
                        try:
                            p.relative_to(repo_root)
                        except ValueError:
                            log.warning(
                                "remote runner: --extra-input-path %s is "
                                "outside project root %s — skipping.",
                                p,
                                repo_root,
                            )
                            continue
                        if not p.exists():
                            log.warning(
                                "remote runner: --extra-input-path %s does "
                                "not exist locally — skipping.",
                                p,
                            )
                            continue
                        if any(existing.local_path == p for existing in push_up):
                            continue
                        push_up.append(
                            InputPath(
                                key_path=f"--extra-input-path={raw}", local_path=p
                            )
                        )
                        log.info(
                            "remote runner: --extra-input-path → %s (will rsync up)",
                            p,
                        )
                    if push_up:
                        sync_input_datasets(host, push_up, repo_root)
                    else:
                        log.info(
                            "remote runner: no local input/output datasets to "
                            "sync (all sources appear to fetch data at runtime)"
                        )
                    # Also push tuned-hyperparameter caches so the remote
                    # can skip retuning when fingerprints match.
                    hp_count = sync_hyperparams_up(
                        host, local_artifacts_root, repo_root
                    )
                    if hp_count:
                        log.info(
                            "remote runner: pushed %d hp/ cache dir(s) — remote "
                            "will reuse tuned params where fingerprints match",
                            hp_count,
                        )
                except Exception as exc:
                    exit_status = "sync_failed"
                    error = f"input dataset sync failed: {exc}"
                    raise

            # ── 3. Bootstrap (system deps + uv install + uv sync) ───────
            log.info("remote runner: bootstrapping remote host")
            rc = ssh_exec(host, system_bootstrap_script(), stream=True)
            if rc != 0:
                exit_status = "sync_failed"
                error = f"system bootstrap exited {rc}"
                raise RuntimeError(error)
            rc = ssh_exec(host, uv_sync_command(extras=opts.uv_extras), stream=True)
            if rc != 0:
                exit_status = "sync_failed"
                error = f"uv sync exited {rc}"
                raise RuntimeError(error)

            # ── 4. Run the pipeline (streaming logs back) ───────────────
            if opts.skip_run:
                log.info(
                    "remote runner: --skip-run set, host is bootstrapped and "
                    "ready but pipeline was not invoked"
                )
                exit_status = "ok"
            else:
                env_prefix = compose_env_prefix(list(opts.forward_env))
                cmd = env_prefix + remote_run_command(
                    pipeline=opts.pipeline,
                    config=opts.config,
                    run_id=opts.run_id,
                    overrides=list(opts.overrides),
                    clean=opts.clean,
                )
                log.info("remote runner: launching pipeline on %s", host.ip)
                rc = ssh_exec(host, cmd, stream=True)

                # ── 5. Artifact + output-dataset sync down ──
                # Always attempt both, even when the pipeline failed —
                # partial outputs are often exactly what the operator wants
                # to inspect to figure out what went wrong.
                try:
                    artifact_bytes = sync_artifacts_back(
                        host, opts.run_id, local_artifacts_root
                    )
                except Exception as exc:
                    log.exception("remote runner: artifact sync-back failed — %s", exc)
                if not opts.skip_input_sync:
                    try:
                        # Pull-back scoped to <region_type> subdir for prep
                        # runs — prevents parallel prep tasks from clobbering
                        # each other's fresh writes via last-writer-wins on
                        # the full prepared_data/ tree. See issue #152.
                        outputs = _load_scoped_output_paths(opts.config, repo_root)
                        if outputs:
                            artifact_bytes += sync_output_datasets_back(
                                host, outputs, repo_root
                            )
                        else:
                            log.info(
                                "remote runner: no config-declared output "
                                "datasets to pull back"
                            )
                    except Exception as exc:
                        log.exception(
                            "remote runner: output-dataset sync-back failed — %s",
                            exc,
                        )

                if rc == 0:
                    exit_status = "ok"
                else:
                    exit_status = "pipeline_failed"
                    error = f"pipeline exited {rc}"

    except Exception as exc:
        log.exception("remote runner: fatal error during run")
        if not error:
            error = f"{type(exc).__name__}: {exc}"
        if exit_status == "unknown":
            exit_status = "provision_failed" if host is None else "pipeline_failed"

    finally:
        if host is not None:
            if opts.keep_alive_on_failure and exit_status != "ok":
                log.warning(
                    "remote runner: --keep-alive-on-failure set and exit_status=%s "
                    "— LEAVING host %s (%s) RUNNING. You are billed until you "
                    "terminate it manually.",
                    exit_status,
                    host.id,
                    host.ip,
                )
            else:
                try:
                    opts.provider.terminate(host)
                except Exception:
                    log.exception(
                        "remote runner: terminate() raised for host=%s — "
                        "check the cloud console",
                        host.id,
                    )

        wall = time.monotonic() - started
        record = RemoteRunRecord(
            run_id=opts.run_id,
            pipeline=opts.pipeline,
            config=opts.config,
            provider=opts.provider.name,
            instance_type=opts.instance_type,
            lifecycle=opts.lifecycle,
            region=opts.region,
            instance_id=host.id if host is not None else "",
            started_at=started_iso,
            ended_at=utc_now_iso(),
            wall_seconds=round(wall, 2),
            exit_status=exit_status,
            artifact_bytes=artifact_bytes,
            error=error,
            extra=dict(host.metadata) if host is not None else {},
        )
        try:
            append_run_record(record, path=opts.ledger_path)
        except Exception:
            log.exception(
                "remote runner: failed to append ledger row — record=%r", record
            )

    return RemoteRunOutcome(
        exit_status=exit_status,
        wall_seconds=round(wall, 2),
        host=host,
        error=error,
        artifact_bytes=artifact_bytes,
    )


def generate_run_id() -> str:
    return uuid.uuid4().hex[:12]


def _load_input_paths(config_path: str, repo_root: Path):
    """Load ``config_path`` as YAML and walk it for local input directories.

    ``config_path`` is resolved relative to ``repo_root`` if not absolute —
    same rule as ``acestor.run`` uses when invoked from the project root.
    """
    raw = _load_config(config_path, repo_root)
    return find_input_paths(raw, repo_root)


def _load_output_paths(config_path: str, repo_root: Path):
    """Same as :func:`_load_input_paths` but for output-tagged keys."""
    raw = _load_config(config_path, repo_root)
    return find_output_paths(raw, repo_root)


def _load_scoped_output_paths(config_path: str, repo_root: Path):
    """Same as :func:`_load_output_paths` but scopes ``prepared_data.base_dir``
    to the ``data.region_type`` subdir if one is set. Used only for the
    pull-back — the push-up path still uses :func:`_load_output_paths` so
    prep still sees the full baseline. See issue #152."""
    raw = _load_config(config_path, repo_root)
    return find_output_paths_scoped_to_region(raw, repo_root)


def _load_config(config_path: str, repo_root: Path) -> dict:
    import yaml  # local import — keeps the module import light

    p = Path(config_path)
    if not p.is_absolute():
        p = repo_root / p
    if not p.is_file():
        raise FileNotFoundError(f"remote runner: config file not found: {p}")
    return yaml.safe_load(p.read_text()) or {}


def _find_repo_root() -> Path:
    """Walk up from this file until we hit the acestor project root.

    Uses pyproject.toml as the marker — it's guaranteed to exist and unique.
    """
    here = Path(__file__).resolve()
    for parent in [here, *here.parents]:
        if (parent / "pyproject.toml").is_file():
            return parent
    raise RuntimeError(
        "remote runner: could not locate project root (no pyproject.toml found "
        "walking up from %s). Set opts.repo_root explicitly." % here
    )

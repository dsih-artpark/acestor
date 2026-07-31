"""Repo + artifact sync helpers built on top of :mod:`acestor.remote.ssh`.

Slice 3 ships repo sync only. Slice 4 will add:

* config-walked local input dataset sync,
* artifact sync-back,
* smarter exclude rules.
"""

from __future__ import annotations

import logging
from pathlib import Path

from acestor.remote.bootstrap import REMOTE_WORKSPACE
from acestor.remote.config_walker import InputPath
from acestor.remote.providers.base import RemoteHost
from acestor.remote.ssh import rsync_down, rsync_up

log = logging.getLogger(__name__)


# Directories/files we NEVER want to push. .venv is many hundreds of MB and
# platform-specific (macOS wheels would fail on Linux anyway). artifacts/ is
# pipeline output — the remote generates its own. __pycache__ is trivially
# regenerated.
_REPO_EXCLUDES = [
    ".venv",
    "artifacts",
    "__pycache__",
    "*.pyc",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "htmlcov",
    ".coverage",
    # Keep .git — reproducibility (remote can print commit sha) is worth
    # the ~few MB. If it becomes an issue we can flip this.
]


def sync_repo(host: RemoteHost, repo_root: Path | str) -> None:
    """Rsync the local repo → ``{workspace}`` on the remote.

    ``--delete`` is on: the remote workspace mirrors the local checkout
    exactly, so re-running the same run-id after local edits doesn't leave
    stale files behind.
    """
    repo_root = Path(repo_root).resolve()
    log.info(
        "sync_repo: %s → %s@%s:%s",
        repo_root,
        host.ssh_user,
        host.ip,
        REMOTE_WORKSPACE,
    )
    rsync_up(
        host=host,
        local_dir=repo_root,
        remote_dir=REMOTE_WORKSPACE,
        exclude=_REPO_EXCLUDES,
        delete=True,
    )


def sync_input_datasets(
    host: RemoteHost,
    inputs: list[InputPath],
    project_root: Path | str,
) -> None:
    """Rsync each discovered local input directory to the same relative path
    on the remote workspace, so the pipeline finds it exactly where the
    config points.

    E.g. local ``<repo>/ap_datasets/geojsons`` → remote
    ``<workspace>/ap_datasets/geojsons``. ``--delete`` is off — inputs
    accumulate across runs on the (in-process) remote, matching local
    behaviour where the operator manages the directory.
    """
    project_root = Path(project_root).resolve()
    for ip in inputs:
        rel = ip.local_path.relative_to(project_root)
        remote_dir = f"{REMOTE_WORKSPACE}/{rel.as_posix()}"
        log.info(
            "sync_input_datasets: [%s] %s → %s@%s:%s",
            ip.key_path,
            ip.local_path,
            host.ssh_user,
            host.ip,
            remote_dir,
        )
        rsync_up(
            host=host,
            local_dir=ip.local_path,
            remote_dir=remote_dir,
            exclude=["__pycache__", "*.pyc"],
            delete=False,
        )


def sync_artifacts_back(
    host: RemoteHost, run_id: str, local_artifacts_root: Path | str
) -> int:
    """Pull ``{workspace}/artifacts/{run_id}/`` → local ``artifacts/{run_id}/``.

    Returns the byte-size of the local run subtree after sync, for the
    ledger. Missing remote directory is treated as "run produced no
    artifacts" — logs but doesn't raise.
    """
    remote_dir = f"{REMOTE_WORKSPACE}/artifacts/{run_id}"
    local_dir = Path(local_artifacts_root) / run_id
    try:
        rsync_down(host=host, remote_dir=remote_dir, local_dir=local_dir)
    except RuntimeError as exc:
        # rsync exit 23 = "some files could not be transferred" — most often
        # the remote source dir didn't exist. Non-fatal for our runner.
        log.warning(
            "sync_artifacts_back: rsync failed (%s) — no artifacts recovered",
            exc,
        )
        return 0
    return _tree_bytes(local_dir)


def _tree_bytes(root: Path) -> int:
    if not root.exists():
        return 0
    total = 0
    for p in root.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total

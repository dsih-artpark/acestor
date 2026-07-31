"""Repo + artifact sync helpers built on top of :mod:`acestor.remote.ssh`.

Slice 3 ships repo sync only. Slice 4 will add:

* config-walked local input dataset sync,
* artifact sync-back,
* smarter exclude rules.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path

from acestor.remote.bootstrap import REMOTE_WORKSPACE
from acestor.remote.config_walker import InputPath
from acestor.remote.providers.base import RemoteHost
from acestor.remote.ssh import rsync_down, rsync_up, ssh_exec

log = logging.getLogger(__name__)


# Directories/files we NEVER want to push. .venv is many hundreds of MB and
# platform-specific (macOS wheels would fail on Linux anyway). artifacts/ is
# pipeline output — the remote generates its own. __pycache__ is trivially
# regenerated. .env / .envrc / *.pem hold secrets — the runner reaches the
# remote pipeline through env_forward.compose_env_prefix, which keeps
# values in-memory only and never touches remote disk. Copying secret files
# would defeat that.
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
    # Secrets — never land these on ephemeral remote disks.
    ".env",
    ".env.*",
    ".envrc",
    "*.pem",
    "*.key",
    # Keep .git — reproducibility (remote can print commit sha) is worth
    # the ~few MB. If it becomes an issue we can flip this.
]


def sync_repo(host: RemoteHost, repo_root: Path | str) -> None:
    """Rsync the local repo → ``{workspace}`` on the remote.

    Uses ``git ls-files --cached --others --exclude-standard`` to pick the
    file set — that's tracked files plus untracked-but-not-gitignored ones.
    Result: the remote gets exactly what a fresh ``git clone`` would give,
    plus any local uncommitted edits, but never scratch data / caches /
    adjacent nested repos even if they happen to live inside ``repo_root``.

    Falls back to a blanket rsync with :data:`_REPO_EXCLUDES` when
    ``repo_root`` isn't a git checkout (unusual but possible in CI-style
    tarball unpacks).
    """
    repo_root = Path(repo_root).resolve()
    log.info(
        "sync_repo: %s → %s@%s:%s",
        repo_root,
        host.ssh_user,
        host.ip,
        REMOTE_WORKSPACE,
    )

    tracked = _git_tracked_and_untracked(repo_root)
    if tracked is None:
        log.warning(
            "sync_repo: %s is not a git checkout — falling back to blanket "
            "rsync with static excludes. May push more than intended.",
            repo_root,
        )
        rsync_up(
            host=host,
            local_dir=repo_root,
            remote_dir=REMOTE_WORKSPACE,
            exclude=_REPO_EXCLUDES,
            delete=True,
        )
        return

    log.info(
        "sync_repo: pushing %d file(s) selected by git ls-files (tracked + "
        "non-gitignored)",
        len(tracked),
    )
    # NUL-separated list so filenames with spaces survive (rsync --from0).
    with tempfile.NamedTemporaryFile(
        mode="wb", suffix=".acestor-files-from", delete=False
    ) as tmp:
        tmp.write(b"\0".join(p.encode("utf-8") for p in tracked))
        files_from = Path(tmp.name)
    try:
        rsync_up(
            host=host,
            local_dir=repo_root,
            remote_dir=REMOTE_WORKSPACE,
            files_from=files_from,
            delete=True,
        )
    finally:
        files_from.unlink(missing_ok=True)


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
    # Ensure the parent directory tree exists on the remote before rsync — the
    # ``*_datasets/`` gitignore rule means these paths weren't created by the
    # earlier repo sync, and rsync refuses to auto-mkdir intermediate parents.
    parents_to_mkdir = {
        f"{REMOTE_WORKSPACE}/{ip.local_path.relative_to(project_root).parent.as_posix()}"
        for ip in inputs
    }
    if parents_to_mkdir:
        mkdir_cmd = "mkdir -p " + " ".join(f"'{p}'" for p in sorted(parents_to_mkdir))
        rc = ssh_exec(host, mkdir_cmd, stream=False)
        if rc != 0:
            raise RuntimeError(
                f"sync_input_datasets: mkdir -p on remote failed (rc={rc})"
            )

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


def sync_output_datasets_back(
    host: RemoteHost,
    outputs: list[InputPath],
    project_root: Path | str,
) -> int:
    """Pull each declared output directory back to its local path.

    Output directories are those flagged by
    :data:`acestor.remote.config_walker.OUTPUT_KEY_PATHS` — currently
    ``data.prepared_data.base_dir`` and
    ``data.weather_download.parsed_output_path``. These are prep-pipeline
    products the downstream pipeline reads; they must land locally or the
    remote run's whole point is lost.

    Returns the total bytes recovered across all outputs, for the ledger.
    Missing remote paths are logged and skipped — non-fatal.
    """
    project_root = Path(project_root).resolve()
    total = 0
    for op in outputs:
        rel = op.local_path.relative_to(project_root)
        remote_dir = f"{REMOTE_WORKSPACE}/{rel.as_posix()}"
        log.info(
            "sync_output_datasets_back: [%s] %s@%s:%s → %s",
            op.key_path,
            host.ssh_user,
            host.ip,
            remote_dir,
            op.local_path,
        )
        try:
            rsync_down(host=host, remote_dir=remote_dir, local_dir=op.local_path)
        except RuntimeError as exc:
            log.warning(
                "sync_output_datasets_back: [%s] failed (%s) — skipping",
                op.key_path,
                exc,
            )
            continue
        total += _tree_bytes(op.local_path)
    return total


def sync_artifacts_back(
    host: RemoteHost, run_id: str, local_artifacts_root: Path | str
) -> int:
    """Pull the whole remote ``{workspace}/artifacts/`` subtree back.

    We rsync the entire directory (not just ``artifacts/{run_id}/``) because
    the actual path a pipeline writes to depends on the config's
    ``storages.artifacts.filesystem.base_path`` — e.g. the prep pipeline
    writes to ``artifacts/gba_ward_prep/{run_id}/``, not
    ``artifacts/{run_id}/``. A fresh ephemeral remote only has *this* run's
    artifacts, so pulling the whole tree is safe and covers arbitrary
    per-pipeline layouts without the caller having to describe them.

    Returns the byte-size of the local artifacts root after sync, for the
    ledger. Missing remote directory is treated as "run produced no
    artifacts" — logs but doesn't raise.
    """
    _ = run_id  # kept in signature for future per-run-id filtering
    remote_dir = f"{REMOTE_WORKSPACE}/artifacts"
    local_dir = Path(local_artifacts_root)
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


def _git_tracked_and_untracked(repo_root: Path) -> list[str] | None:
    """Return the file list ``git ls-files`` gives us for ``repo_root``.

    ``-z`` (NUL-separated) so filenames with spaces / newlines round-trip.
    Returns ``None`` when ``repo_root`` isn't a git checkout — caller falls
    back to a blanket rsync.
    """
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout
    if not raw:
        return []
    # Trailing NUL is normal — strip before split so we don't emit an empty entry.
    return [p.decode("utf-8") for p in raw.rstrip(b"\0").split(b"\0") if p]


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

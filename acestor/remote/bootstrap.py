"""Composes the shell commands that bootstrap a fresh remote host.

Two phases, both invoked from :func:`acestor.remote.runner.run_remote`:

1. :func:`system_bootstrap_script` — the *first* thing that runs after ssh
   is up. Installs OS packages that geopandas/pandas need to compile
   (libgeos, libproj, libgdal), then installs uv.
2. :func:`uv_sync_command` — after the repo has been rsync'd over, run
   ``uv sync`` in the workspace so the pipeline's Python deps are ready.

Kept as pure string composition so unit tests can pin the shell command
exactly and catch accidental changes to the bootstrap contract.
"""

from __future__ import annotations

REMOTE_WORKSPACE = "/home/ubuntu/acestor"


def system_bootstrap_script() -> str:
    """One-shot shell script: system packages + uv install.

    Deliberately idempotent — safe to re-run if the runner retries. ``apt``
    is set non-interactive so unattended-upgrades / conf prompts can't stall
    the run.
    """
    return _SYSTEM_BOOTSTRAP


def uv_sync_command(workspace: str = REMOTE_WORKSPACE, extras: str = "all") -> str:
    """Run inside the pushed repo: ``uv sync`` with the chosen extras.

    ``extras='all'``  → ``uv sync --all-extras`` (matches CLAUDE.md's rule)
    ``extras='none'`` → ``uv sync`` (base install only, for quick tests)
    otherwise         → passed through verbatim as ``--extra <name>``
    """
    if extras == "all":
        flag = "--all-extras"
    elif extras == "none":
        flag = ""
    else:
        flag = f"--extra {extras}"
    return (
        f'export PATH="$HOME/.local/bin:$PATH" && '
        f"cd {workspace} && uv sync {flag}".rstrip()
    )


def remote_run_command(
    pipeline: str,
    config: str,
    run_id: str,
    overrides: list[str] | None = None,
    clean: bool = False,
    workspace: str = REMOTE_WORKSPACE,
) -> str:
    """Compose the remote ``python -m acestor.run …`` invocation.

    Mirrors ``acestor.run``'s CLI surface. Wrapped in ``bash -lc`` upstream
    so PATH picks up the just-installed uv.
    """
    parts = [
        f"cd {workspace}",
        "&&",
        'export PATH="$HOME/.local/bin:$PATH"',
        "&&",
        "uv run python -m acestor.run",
        f"--pipeline {_sh_quote(pipeline)}",
        f"--config {_sh_quote(config)}",
        f"--run-id {_sh_quote(run_id)}",
    ]
    for kv in overrides or []:
        parts.append(f"--set {_sh_quote(kv)}")
    if clean:
        parts.append("--clean")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _sh_quote(s: str) -> str:
    """Minimal shell-safe quoting — good enough for well-formed CLI values.

    We control the values (they come from our own CLI parser or config
    walker), so full POSIX shell escaping is overkill. Single-quote and
    escape any embedded single quotes.
    """
    return "'" + s.replace("'", "'\\''") + "'"


_SYSTEM_BOOTSTRAP = r"""set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# Give cloud-init a moment to release the apt lock on a freshly-booted image
for i in 1 2 3 4 5; do
  if sudo -n fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1; then
    echo "bootstrap: waiting for apt lock ($i/5)"; sleep 5
  else
    break
  fi
done

sudo -E apt-get update -qq
sudo -E apt-get install -yqq --no-install-recommends \
  curl ca-certificates git build-essential rsync \
  libgeos-dev libproj-dev libgdal-dev libspatialindex-dev libhdf5-dev libnetcdf-dev

# Idempotent uv install — the installer script is a no-op if uv is current.
if ! command -v uv >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/uv" ]; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi

export PATH="$HOME/.local/bin:$PATH"
uv --version
"""

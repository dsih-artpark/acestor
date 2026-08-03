"""Forward selected env vars into the remote pipeline invocation.

Dashboard-source runs need ``DASHBOARD_URL`` + ``DASHBOARD_CLIENT_ID`` +
``DASHBOARD_CLIENT_SECRET`` on the remote. We deliberately do NOT copy
``.env`` files to the remote disk — secrets shouldn't survive on ephemeral
instances a shutdown-scan away from disclosure. Instead we resolve values
from the local environment and inject them into the ssh exec command via
``env NAME='value' …``, so they live only in the remote process's memory.

The commonly-forwarded set is exposed as :data:`DEFAULT_FORWARD_ENV` so the
CLI can auto-forward without the operator listing them every time.
"""

from __future__ import annotations

import logging
import os
import shlex

log = logging.getLogger(__name__)


# Auto-forwarded when the operator doesn't override. Add here as new
# integrations land; each name is only forwarded if actually set locally.
DEFAULT_FORWARD_ENV: tuple[str, ...] = (
    "DASHBOARD_URL",
    "DASHBOARD_CLIENT_ID",
    "DASHBOARD_CLIENT_SECRET",
    "DASHBOARD_SELECTED_REGION_ID",
    # CDS + OpenMeteo used by the historical / legacy weather paths
    "CDSAPI_URL",
    "CDSAPI_KEY",
)


def compose_env_prefix(names: list[str] | tuple[str, ...]) -> str:
    """Return a shell prefix ``export NAME='val'; export NAME2='val2'; ``
    or the empty string if none of the requested names are set.

    Shell ``export`` is used rather than the ``env`` binary because the
    remote command that follows starts with shell builtins (``cd``, ``&&``,
    ``export PATH``) — ``env`` would refuse to exec ``cd`` since it isn't a
    real binary. ``export`` runs inside the shell, so the vars are visible
    to every subsequent command in the compound line.

    Skips names that aren't set locally — silently, since half the auto-
    forwarded set is optional depending on which pipeline you're running.
    Logs at INFO with names + a redacted preview so operators can confirm
    what actually got forwarded without leaking secret values.
    """
    resolved: list[tuple[str, str]] = []
    skipped: list[str] = []
    for name in names:
        val = os.environ.get(name)
        if val is None or val == "":
            skipped.append(name)
            continue
        resolved.append((name, val))

    if skipped:
        log.info(
            "env_forward: skipping %d unset var(s) — %s",
            len(skipped),
            ", ".join(skipped),
        )
    if not resolved:
        return ""

    log.info(
        "env_forward: forwarding %d var(s) → %s",
        len(resolved),
        ", ".join(f"{n}={_redact(v)}" for n, v in resolved),
    )

    parts = [f"export {n}={shlex.quote(v)};" for n, v in resolved]
    return " ".join(parts) + " "


def _redact(v: str) -> str:
    """Fingerprint for logs: length + first/last 2 chars. Never the full value."""
    if len(v) <= 6:
        return "***"
    return f"{v[:2]}…{v[-2:]} ({len(v)}c)"

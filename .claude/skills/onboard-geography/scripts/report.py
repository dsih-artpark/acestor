"""Check results and the audit trail written for each onboarding stage."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

PASS, WARN, FAIL = "pass", "warn", "fail"


@dataclass
class Check:
    """One deterministic check. fail blocks the next stage; warn does not."""

    id: str
    status: str
    message: str
    detail: dict = field(default_factory=dict)


def failed(checks: list[Check]) -> bool:
    return any(check.status == FAIL for check in checks)


def summarise(checks: list[Check]) -> dict:
    counts = {s: sum(c.status == s for c in checks) for s in (PASS, WARN, FAIL)}
    return {
        "ready": counts[FAIL] == 0,
        "counts": counts,
        "checks": [asdict(check) for check in checks],
    }


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def code_version() -> str:
    """Commit of the repository the stage ran from, for reproducing a report."""
    result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=False
    )
    return result.stdout.strip() or "unknown"


def write_audit(workdir: Path, stage: str, report: dict, inputs: list[Path]) -> Path:
    """Save a stage's report with what it read, so a result can be traced back."""
    folder = workdir / "onboarding"
    folder.mkdir(parents=True, exist_ok=True)
    record = {
        "stage": stage,
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "code_version": code_version(),
        "inputs": {str(p): sha256(p) for p in inputs if p.is_file()},
        **report,
    }
    path = folder / f"{stage}.json"
    if path.exists():
        # A rerun does not erase the earlier report: a failed first pass is evidence too.
        earlier = len(list(folder.glob(f"{stage}.run*.json"))) + 1
        path.rename(folder / f"{stage}.run{earlier}.json")
    path.write_text(json.dumps(record, indent=2, default=str) + "\n")
    return path

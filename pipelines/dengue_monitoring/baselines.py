"""Immutable, content-addressed calibration bundles and persistent run claims."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path


def sha(data: bytes | str) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def settings(cfg: dict) -> tuple[Path, str]:
    options = cfg.get("baseline", {})
    directory = Path(
        options.get("directory", "artifacts/monitoring_baselines")
    ).resolve()
    name = options.get("name", "default")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ValueError("baseline.name must contain only letters, numbers, _ or -")
    baseline_id = options.get("id")
    if baseline_id and not re.fullmatch(r"[0-9a-f]{64}", str(baseline_id)):
        raise ValueError("baseline.id must be a full SHA-256 identifier")
    return directory, name


def load(cfg: dict) -> tuple[str, dict, dict[str, bytes]]:
    root, name = settings(cfg)
    identity = cfg.get("baseline", {}).get("id")
    if not identity:
        pointer = root / "active" / f"{name}.json"
        if not pointer.is_file():
            raise ValueError(
                "No frozen baseline exists. Run explicitly with monitoring.mode=build_baseline first"
            )
        identity = json.loads(pointer.read_text())["baseline_id"]
    if not re.fullmatch(r"[0-9a-f]{64}", str(identity)):
        raise ValueError("Invalid stored baseline identifier")
    manifest = json.loads((root / identity / "manifest.json").read_text())
    payload = manifest["identity"]
    if sha(canonical(payload)) != identity or manifest["baseline_id"] != identity:
        raise ValueError("Frozen baseline manifest integrity check failed")
    if payload.get("schema_version") != 2:
        raise ValueError("Unsupported frozen baseline schema_version")
    if payload.get("contract", {}).get("method_version") != 1:
        raise ValueError("Unsupported frozen baseline method_version")
    files = {}
    for relative, expected in payload["files"].items():
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("Invalid baseline bundle path")
        content = (root / identity / path).read_bytes()
        if sha(content) != expected:
            raise ValueError(f"Frozen baseline integrity check failed: {relative}")
        files[relative] = content
    return identity, payload, files


def save(cfg: dict, contract: dict, provenance: list, files: dict[str, bytes]) -> str:
    root, name = settings(cfg)
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 2,
        "contract": contract,
        "inputs": provenance,
        "files": {key: sha(value) for key, value in sorted(files.items())},
    }
    identity = sha(canonical(payload))
    manifest = {
        "baseline_id": identity,
        "identity": payload,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    # Publish only complete bundles. Identical content reuses its existing SHA;
    # changed content always gets another directory, never overwriting a version.
    with tempfile.TemporaryDirectory(prefix=".building-", dir=root) as temp:
        staging = Path(temp) / "bundle"
        staging.mkdir()
        for relative, content in files.items():
            path = staging / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        (staging / "manifest.json").write_text(canonical(manifest) + "\n")
        try:
            os.rename(staging, root / identity)
        except OSError:
            if not (root / identity).is_dir():
                raise
            # Existing content must still verify before it can be selected.
            load({**cfg, "baseline": {**cfg.get("baseline", {}), "id": identity}})
    return identity


def activate(cfg: dict, identity: str) -> None:
    root, name = settings(cfg)
    load({**cfg, "baseline": {**cfg.get("baseline", {}), "id": identity}})
    active = root / "active"
    active.mkdir(exist_ok=True)
    pointer = active / f".{name}-{uuid.uuid4().hex}.tmp"
    try:
        pointer.write_text(canonical({"baseline_id": identity}) + "\n")
        os.replace(pointer, active / f"{name}.json")
    finally:
        pointer.unlink(missing_ok=True)


def claim_path(raw: dict, run_id: str) -> Path:
    root, _ = settings(raw["monitoring"])
    storage = raw.get("storages", {}).get("artifacts", {})
    if storage.get("kind", "filesystem") == "filesystem":
        destination = str(Path(storage["filesystem"]["base_path"]).resolve())
    else:
        destination = canonical(storage)
    return root / "run_claims" / f"{sha(destination + ':' + run_id)}.json"


def preflight(raw: dict, run_id: str, clean: bool = False) -> None:
    if clean:
        raise ValueError(
            "Monitoring runs are immutable; --clean is not supported. Use a new run ID"
        )
    if not run_id or Path(run_id).name != run_id or run_id in (".", ".."):
        raise ValueError("Monitoring run IDs must be a single path-safe name")
    if claim_path(raw, run_id).exists():
        raise ValueError(
            f"Monitoring run ID {run_id!r} has already been used; choose a new ID"
        )
    storage = raw.get("storages", {}).get("artifacts", {})
    if storage.get("kind", "filesystem") == "filesystem":
        directory = Path(storage["filesystem"]["base_path"]) / run_id
        if any((directory / child).exists() for child in ("outputs", "inputs")):
            raise ValueError(
                f"Monitoring artifacts already exist for {run_id!r}; choose a new ID"
            )


def claim(raw: dict, run_id: str) -> dict:
    preflight(raw, run_id)
    path = claim_path(raw, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "run_id": run_id,
        "execution_id": str(uuid.uuid4()),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "mode": raw["monitoring"].get("mode", "monitor"),
    }
    try:
        with path.open("x") as stream:
            stream.write(canonical(record) + "\n")
    except FileExistsError as exc:
        raise ValueError(
            f"Monitoring run ID {run_id!r} has already been claimed"
        ) from exc
    return record

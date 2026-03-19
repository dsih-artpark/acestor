"""Minimal FastAPI app for inspecting acestor runs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException


def _load_run_dirs(base_path: Path) -> List[Path]:
    if not base_path.exists():
        return []
    return [p for p in base_path.iterdir() if p.is_dir()]


def _load_run_json(run_dir: Path) -> Dict[str, Any]:
    run_file = run_dir / "run.json"
    if not run_file.exists():
        raise FileNotFoundError(run_file)
    with run_file.open("r", encoding="utf-8") as f:
        return json.load(f)


def create_app(runs_base: str = "./runs") -> FastAPI:
    """Create a FastAPI app for listing and inspecting runs.

    Parameters
    ----------
    runs_base:
        Filesystem path where per-run directories are stored. Each run is
        expected to live under ``<runs_base>/<run_id>/`` with a ``run.json``
        metadata file created by ``PipelineRunner``.
    """
    app = FastAPI(title="acestor runs")
    base_path = Path(runs_base)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/runs")
    def list_runs() -> List[Dict[str, Any]]:
        """List available runs by reading run.json files under runs_base."""
        runs: List[Dict[str, Any]] = []
        for run_dir in _load_run_dirs(base_path):
            try:
                data = _load_run_json(run_dir)
                runs.append(
                    {
                        "run_id": data.get("run_id") or run_dir.name,
                        "pipeline": data.get("pipeline"),
                        "status": data.get("status"),
                        "started_at": data.get("started_at"),
                        "finished_at": data.get("finished_at"),
                    }
                )
            except Exception:
                # Skip malformed runs quietly for now.
                continue
        # Sort most recent first by finished_at if available.
        runs.sort(
            key=lambda r: (r.get("finished_at") or "", r.get("run_id") or ""),
            reverse=True,
        )
        return runs

    @app.get("/runs/{run_id}")
    def get_run(run_id: str) -> Dict[str, Any]:
        """Return detailed information for a single run."""
        run_dir = base_path / run_id
        if not run_dir.exists() or not run_dir.is_dir():
            raise HTTPException(status_code=404, detail="Run not found")
        try:
            data = _load_run_json(run_dir)
        except FileNotFoundError:
            raise HTTPException(
                status_code=404, detail="run.json not found for this run"
            )
        return data

    return app

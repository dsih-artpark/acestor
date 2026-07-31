"""Walk a pipeline YAML to find local filesystem inputs the remote needs.

The remote runner has to know *which* local directories to rsync up before
the pipeline runs. Rather than hardcode paths per pipeline, we scan the
config for the well-known input keys the prep + main pipelines use.

Input-ish keys (values are directories that must exist locally and be
readable by the pipeline on the remote):

    data.geojson.base_path
    data.case_download.source_path
    data.weather_download.source_path
    data.weather_download.filesystem_base_path
    data.weather_download.netcdf_cache_path
    data.prepared_data.base_dir

Output-ish keys are deliberately excluded — the remote generates its own:

    data.weather_download.parsed_output_path
    storages.artifacts.filesystem.base_path

If a discovered path doesn't exist locally, we skip it — that most likely
means the pipeline will fetch the data itself (e.g. ``source_mode:
dashboard``), and forcing an rsync would just fail.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

log = logging.getLogger(__name__)


# Ordered so config walkers scan predictable, stable locations first.
INPUT_KEY_PATHS: tuple[tuple[str, ...], ...] = (
    ("data", "geojson", "base_path"),
    ("data", "case_download", "source_path"),
    ("data", "weather_download", "source_path"),
    ("data", "weather_download", "filesystem_base_path"),
    ("data", "weather_download", "netcdf_cache_path"),
    ("data", "prepared_data", "base_dir"),
)


@dataclass(frozen=True)
class InputPath:
    """A local input directory the remote needs.

    ``key_path`` is the dotted YAML location the value came from — used in
    log lines so operators can trace *why* a directory got synced.
    """

    key_path: str
    local_path: Path  # absolute, resolved


def find_input_paths(
    config: Mapping[str, Any], project_root: Path | str
) -> list[InputPath]:
    """Return the set of local directories referenced by ``config`` that
    exist on disk and should be rsync'd up to the remote.

    Relative paths in the YAML are resolved against ``project_root`` — this
    matches acestor's own behaviour (``python -m acestor.run`` is always
    invoked from the project root).
    """
    project_root = Path(project_root).resolve()
    seen: set[Path] = set()
    out: list[InputPath] = []

    for key_tuple in INPUT_KEY_PATHS:
        raw = _lookup(config, key_tuple)
        if not isinstance(raw, str) or not raw.strip():
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = (project_root / path).resolve()
        else:
            path = path.resolve()

        dotted = ".".join(key_tuple)
        if not path.exists():
            log.debug(
                "config_walker: %s → %s does not exist locally, skipping "
                "(pipeline is likely fetching this at runtime)",
                dotted,
                path,
            )
            continue
        if not path.is_dir():
            log.warning(
                "config_walker: %s → %s exists but is not a directory, skipping",
                dotted,
                path,
            )
            continue
        # Refuse to sync paths outside the project root — no ~/.aws surprises.
        try:
            path.relative_to(project_root)
        except ValueError:
            log.warning(
                "config_walker: %s → %s is OUTSIDE project root %s — skipping "
                "for safety. Move the data under the project or pass an "
                "explicit --extra-sync flag if you really want this.",
                dotted,
                path,
                project_root,
            )
            continue

        if path in seen:
            continue
        seen.add(path)
        out.append(InputPath(key_path=dotted, local_path=path))
        log.info("config_walker: %s → %s (will rsync up)", dotted, path)

    return out


def _lookup(d: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    cur: Any = d
    for k in keys:
        if not isinstance(cur, Mapping) or k not in cur:
            return None
        cur = cur[k]
    return cur

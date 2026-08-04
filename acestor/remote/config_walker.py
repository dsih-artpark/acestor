"""Walk a pipeline YAML to find local filesystem paths the remote needs.

The remote runner has to know *which* local directories to rsync — both
inputs to push up before the run, and outputs to pull back after. Rather
than hardcode paths per pipeline, we scan the config for the well-known
keys the prep + main pipelines use.

**Input-ish keys** (push up before the run; the pipeline reads from them):

    data.geojson.base_path
    data.weather_download.source_path
    data.weather_download.filesystem_base_path
    data.weather_download.netcdf_cache_path

**Output-ish keys** (also push up so incremental caches carry over — then
pull back after the run so the local machine gets the freshly-appended
data). The prep pipeline treats ``prepared_data`` as a reusable cache
that downstream pipelines consume; without pulling it back the whole
point of running prep remotely is lost.

    data.prepared_data.base_dir
    data.weather_download.parsed_output_path
    data.case_download.source_path

``case_download.source_path`` lives in OUTPUT (not INPUT) because the
dashboard case source *writes* freshly-fetched xlsx files there and its
containment/trim logic (see PR #108) does incremental fetches when
prior-run xlsx files are present. Pulling it back lets the next run's
push-up phase seed those xlsx files, so subsequent nightly runs
only fetch the delta instead of re-streaming years of history.

``storages.artifacts.filesystem.base_path`` is handled separately by
:func:`acestor.remote.sync.sync_artifacts_back`, which pulls the entire
remote ``artifacts/`` subtree — arbitrary per-pipeline sub-layouts don't
have to be enumerated here.

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
    ("data", "weather_download", "source_path"),
    ("data", "weather_download", "filesystem_base_path"),
    ("data", "weather_download", "netcdf_cache_path"),
)

# Output-ish keys — pushed up (so incremental caches survive) AND pulled
# back (so the local machine gets whatever the pipeline appended).
OUTPUT_KEY_PATHS: tuple[tuple[str, ...], ...] = (
    ("data", "prepared_data", "base_dir"),
    ("data", "weather_download", "parsed_output_path"),
    ("data", "case_download", "source_path"),
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
    return _resolve_key_paths(config, project_root, INPUT_KEY_PATHS, "up")


def find_output_paths(
    config: Mapping[str, Any], project_root: Path | str
) -> list[InputPath]:
    """Return the set of local *output* directories the runner must pull
    back from the remote after the pipeline exits.

    Same resolution rules as :func:`find_input_paths`. A local dir doesn't
    have to exist yet — the runner creates it during pull-back. But we skip
    values pointing outside ``project_root`` (safety) and empty strings.
    """
    return _resolve_key_paths(
        config, project_root, OUTPUT_KEY_PATHS, "down", require_exists=False
    )


def _resolve_key_paths(
    config: Mapping[str, Any],
    project_root: Path | str,
    key_paths: tuple[tuple[str, ...], ...],
    direction: str,  # "up" or "down" — purely for log wording
    require_exists: bool = True,
) -> list[InputPath]:
    project_root = Path(project_root).resolve()
    seen: set[Path] = set()
    out: list[InputPath] = []

    for key_tuple in key_paths:
        raw = _lookup(config, key_tuple)
        if not isinstance(raw, str) or not raw.strip():
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = (project_root / path).resolve()
        else:
            path = path.resolve()

        dotted = ".".join(key_tuple)
        if require_exists and not path.exists():
            log.debug(
                "config_walker: %s → %s does not exist locally, skipping "
                "(pipeline is likely fetching this at runtime)",
                dotted,
                path,
            )
            continue
        if path.exists() and not path.is_dir():
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
                "for safety.",
                dotted,
                path,
                project_root,
            )
            continue

        if path in seen:
            continue
        seen.add(path)
        out.append(InputPath(key_path=dotted, local_path=path))
        log.info("config_walker: %s → %s (will rsync %s)", dotted, path, direction)

    return out


def _lookup(d: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    cur: Any = d
    for k in keys:
        if not isinstance(cur, Mapping) or k not in cur:
            return None
        cur = cur[k]
    return cur

"""Pipeline runtime context for acestor."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping

from acestor.core.config import PipelineConfig
from acestor.io import Storage, FileStorage, S3Storage
from acestor.infra import create_logger


@dataclass
class PipelineContext:
    """Holds run-wide objects such as config, storages and logger."""

    config: Mapping[str, Any]
    run_id: str
    storages: Dict[str, Storage] = field(default_factory=dict)
    logger: Any | None = None

    def require_storage(self, name: str) -> Storage:
        """Return a configured storage or raise a clear error."""
        storage = self.storages.get(name)
        if storage is None:
            raise ValueError(
                f"Missing required storage {name!r} in PipelineContext.storages."
            )
        return storage

    def config_section(self, path: str) -> Mapping[str, Any]:
        """Return a nested config mapping for a dotted path, or {} if absent."""
        current: Any = self.config
        for part in path.split("."):
            if not isinstance(current, Mapping):
                return {}
            current = current.get(part)
            if current is None:
                return {}
        return current if isinstance(current, Mapping) else {}

    def artifact_path(self, relpath: str) -> str:
        """Return the full artifact key for a relative path under this run."""
        return f"{self.run_id}/{relpath}"

    def write_artifact_json(self, relpath: str, payload: Mapping[str, Any]) -> str:
        """Write a JSON payload under the current run in the artifacts storage."""
        out_path = self.artifact_path(relpath)
        self.artifacts.write_json(dict(payload), out_path)
        return out_path

    @property
    def artifacts(self) -> Storage:
        """Convenience accessor for the ``artifacts`` storage."""
        return self.require_storage("artifacts")

    @property
    def log(self) -> logging.Logger:
        """Always-available logger (falls back to a no-op logger if unset)."""
        if self.logger is not None:
            return self.logger
        lg = logging.getLogger("acestor")
        if not any(isinstance(h, logging.NullHandler) for h in lg.handlers):
            lg.addHandler(logging.NullHandler())
        return lg

    @classmethod
    def from_config(cls, config: PipelineConfig, run_id: str) -> "PipelineContext":
        raw = dict(config.raw)
        storages_cfg = raw.get("storages", {}) or {}

        storages: Dict[str, Storage] = {}
        for name, scfg in storages_cfg.items():
            kind = (scfg.get("kind") or "filesystem").lower()
            if kind == "filesystem":
                fs_cfg = scfg.get("filesystem") or {}
                base_path = fs_cfg.get("base_path")
                if not base_path:
                    raise ValueError(
                        f"Storage {name!r} of kind 'filesystem' requires 'filesystem.base_path'."
                    )
                storages[name] = FileStorage(base_path=Path(base_path))
            elif kind == "s3":
                s3_cfg = scfg.get("s3") or {}
                bucket = s3_cfg.get("bucket")
                if not bucket:
                    raise ValueError(
                        f"Storage {name!r} of kind 's3' requires 's3.bucket'."
                    )
                base_prefix = s3_cfg.get("base_prefix", "")
                profile = s3_cfg.get("aws_profile")
                region = s3_cfg.get("region")
                storages[name] = S3Storage(
                    bucket=bucket,
                    base_prefix=base_prefix,
                    profile=profile,
                    region=region,
                )
            else:
                raise ValueError(f"Unknown storage kind {kind!r} for storage {name!r}.")

        logging_cfg = raw.get("logging") or {}
        logger: Any | None = None
        if logging_cfg:
            pipeline_name = (raw.get("pipeline") or {}).get("name") or "pipeline"
            logger_name = f"acestor.{pipeline_name}"
            logger = create_logger(logger_name, run_dir=None)
            level_str = (logging_cfg.get("level") or "INFO").upper()
            try:
                logger.setLevel(getattr(logging, level_str, logging.INFO))
            except Exception:
                pass

        return cls(config=config.raw, run_id=run_id, storages=storages, logger=logger)

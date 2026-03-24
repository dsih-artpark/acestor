from __future__ import annotations

import os
from typing import Any
from typing import ClassVar

from acestor import BaseStep, NoInputs, PipelineContext
from acestor.core.sources import FileSystemSource, S3Source
from pipelines.gba_dengue.configs import CaseDownloadConfig, _section
from pipelines.gba_dengue.results import CaseDownloadResult


class DownloadCaseDataStep(BaseStep[NoInputs, CaseDownloadResult]):
    input_type: ClassVar[type] = NoInputs

    def __init__(self, source: Any | None = None) -> None:
        if source is not None:
            self.source = source
            return
        self.source = None

    def _build_source(self, cfg: CaseDownloadConfig) -> Any | None:
        backend = (cfg.source_backend or "filesystem").strip().lower()
        source_path = (cfg.source_path or "").strip()

        if source_path and backend == "s3" and source_path.startswith("s3://"):
            bucket_and_prefix = source_path[len("s3://") :]
            bucket, _, prefix = bucket_and_prefix.partition("/")
            return S3Source(
                bucket=bucket,
                base_prefix=prefix,
                aws_profile=(os.getenv("AWS_PROFILE", "").strip() or None),
                region=(os.getenv("AWS_REGION", "").strip() or None),
                cache_enabled=cfg.cache_enabled,
                cache_dir=cfg.cache_dir,
                strategy=cfg.cache_strategy,
            )

        if source_path and backend == "filesystem":
            return FileSystemSource(base_path=source_path)

        if backend == "s3":
            if cfg.s3_bucket:
                return S3Source(
                    bucket=cfg.s3_bucket,
                    base_prefix=cfg.s3_prefix,
                    aws_profile=(os.getenv("AWS_PROFILE", "").strip() or None),
                    region=(os.getenv("AWS_REGION", "").strip() or None),
                    cache_enabled=cfg.cache_enabled,
                    cache_dir=cfg.cache_dir,
                    strategy=cfg.cache_strategy,
                )
            return None

        if cfg.filesystem_base_path:
            return FileSystemSource(base_path=cfg.filesystem_base_path)
        return None

    def run(self, context: PipelineContext, inputs: NoInputs) -> CaseDownloadResult:
        cfg = CaseDownloadConfig.from_raw(
            _section(context.config, "data.case_download")
        )

        if not cfg.enabled:
            context.log.info("download_case_data: disabled")
            return CaseDownloadResult(enabled=False)

        source = self.source or self._build_source(cfg)
        if source is None:
            source = context.require_storage(cfg.source_storage)
        paths = cfg.source_paths
        if not paths:
            paths = source.list_objects(cfg.source_prefix)
        # S3 listings can include empty "folder marker" objects; skip them.
        paths = [p for p in paths if str(p).strip() and not str(p).endswith("/")]

        copied: list[str] = []
        backend = (cfg.source_backend or "filesystem").strip().lower()
        for src in paths:
            try:
                data = source.read(src)
            except FileNotFoundError as exc:
                raise FileNotFoundError(
                    "download_case_data: source file not found. "
                    f"backend={backend}, source_path={src!r}, "
                    f"filesystem_base_path={cfg.filesystem_base_path!r}, "
                    f"s3_bucket={cfg.s3_bucket!r}, s3_prefix={cfg.s3_prefix!r}. "
                    "Update data.case_download in YAML (filesystem_base_path/source_paths) "
                    "or choose source_backend='s3'."
                ) from exc
            if not data:
                raise ValueError(f"download_case_data: source file is empty: {src!r}")
            copied.append(f"{backend}://{src}")

        context.log.info(
            "download_case_data: Referencing data from the disk, validated %d non-empty files (backend=%s)",
            len(copied),
            backend,
        )
        return CaseDownloadResult(enabled=True, copied_files=copied)

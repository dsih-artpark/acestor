from __future__ import annotations

import os
from typing import Any, ClassVar

from acestor import BaseStep, NoInputs, PipelineContext
from acestor.core.sources import FileSystemSource, S3Source
from pipelines.dengue_prep.configs import PrepCaseDownloadConfig, _section
from pipelines.dengue_prep.results import PrepCaseDownloadResult


class PrepDownloadCaseDataStep(BaseStep[NoInputs, PrepCaseDownloadResult]):
    input_type: ClassVar[type] = NoInputs

    def __init__(self, source: Any | None = None) -> None:
        self.source = source

    def _build_source(self, cfg: PrepCaseDownloadConfig) -> Any | None:
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
        if backend == "s3" and cfg.s3_bucket:
            return S3Source(
                bucket=cfg.s3_bucket,
                base_prefix=cfg.s3_prefix,
                aws_profile=(os.getenv("AWS_PROFILE", "").strip() or None),
                region=(os.getenv("AWS_REGION", "").strip() or None),
                cache_enabled=cfg.cache_enabled,
                cache_dir=cfg.cache_dir,
                strategy=cfg.cache_strategy,
            )
        if cfg.filesystem_base_path:
            return FileSystemSource(base_path=cfg.filesystem_base_path)
        return None

    def run(self, context: PipelineContext, inputs: NoInputs) -> PrepCaseDownloadResult:
        cfg = PrepCaseDownloadConfig.from_raw(
            _section(context.config, "data.case_download")
        )
        if not cfg.enabled:
            context.log.info("prep download_case_data: disabled")
            return PrepCaseDownloadResult(enabled=False)

        source = self.source or self._build_source(cfg)
        if source is None:
            if not cfg.source_storage:
                raise ValueError(
                    "prep download_case_data: no case source configured. "
                    "Set the DENGUE_PREP_CASE_SOURCE environment variable to the path "
                    "of your raw case data directory, or set data.case_download.source_path "
                    "in the pipeline config."
                )
            context.log.info(
                "prep download_case_data: no direct source built — using storage %r",
                cfg.source_storage,
            )
            source = context.require_storage(cfg.source_storage)

        backend = (cfg.source_backend or "filesystem").strip().lower()
        context.log.info(
            "prep download_case_data: backend=%s source_path=%r prefix=%r",
            backend,
            cfg.source_path or "",
            cfg.source_prefix or "",
        )

        raw_paths = cfg.source_paths or source.list_objects(cfg.source_prefix)
        paths = [p for p in raw_paths if str(p).strip() and not str(p).endswith("/")]
        if len(paths) != len(list(raw_paths)):
            context.log.debug(
                "prep download_case_data: filtered %d raw paths → %d valid file paths "
                "(removed empty/directory entries)",
                len(list(raw_paths)),
                len(paths),
            )

        copied: list[str] = []
        for src in paths:
            context.log.debug("prep download_case_data: reading %r", src)
            try:
                data = source.read(src)
            except FileNotFoundError as exc:
                raise FileNotFoundError(
                    f"prep download_case_data: source file not found: {src!r} "
                    f"(backend={backend})"
                ) from exc
            if not data:
                raise ValueError(
                    f"prep download_case_data: source file is empty: {src!r}"
                )
            copied.append(f"{backend}://{src}")

        context.log.info(
            "prep download_case_data: validated %d files (backend=%s)",
            len(copied),
            backend,
        )
        return PrepCaseDownloadResult(enabled=True, copied_files=copied)

from __future__ import annotations

import os
from typing import Any
from typing import ClassVar

from acestor import BaseStep, NoInputs, PipelineContext
from pipelines.gba_dengue.configs import CaseDownloadConfig, _section
from pipelines.gba_dengue.sources import filesystem as fs_sources
from pipelines.gba_dengue.sources import s3 as s3_sources
from pipelines.gba_dengue.results import CaseDownloadResult


class DownloadCaseDataStep(BaseStep[NoInputs, CaseDownloadResult]):
    input_type: ClassVar[type] = NoInputs

    def __init__(self, source: Any | None = None) -> None:
        if source is not None:
            self.source = source
            return
        backend = os.getenv("GBA_CASE_SOURCE_BACKEND", "filesystem").strip().lower()
        if backend == "s3":
            try:
                self.source = s3_sources.get_case_source()
                return
            except Exception:
                # Fall back to legacy storage wiring when env is incomplete.
                self.source = None
                return
        self.source = fs_sources.get_case_source()

    def run(self, context: PipelineContext, inputs: NoInputs) -> CaseDownloadResult:
        cfg = CaseDownloadConfig.from_raw(
            _section(context.config, "data.case_download")
        )

        if not cfg.enabled:
            context.log.info("download_case_data: disabled")
            return CaseDownloadResult(enabled=False)

        source = self.source
        if source is None:
            source = context.require_storage(cfg.source_storage)
        paths = cfg.source_paths
        if not paths:
            paths = source.list_objects(cfg.source_prefix)

        copied: list[str] = []
        for src in paths:
            filename = src.replace("\\", "/").split("/")[-1]
            dest = context.artifact_path(f"{cfg.dest_relpath}/{filename}")
            context.artifacts.write(source.read(src), dest)
            copied.append(dest)

        context.log.info("download_case_data: copied %d files", len(copied))
        return CaseDownloadResult(enabled=True, copied_files=copied)

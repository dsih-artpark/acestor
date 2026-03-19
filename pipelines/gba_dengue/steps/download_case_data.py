from __future__ import annotations

from typing import ClassVar

from acestor import BaseStep, NoInputs, PipelineContext
from pipelines.gba_dengue.configs import CaseDownloadConfig, _section
from pipelines.gba_dengue.results import CaseDownloadResult


class DownloadCaseDataStep(BaseStep[NoInputs, CaseDownloadResult]):
    input_type: ClassVar[type] = NoInputs

    def run(self, context: PipelineContext, inputs: NoInputs) -> CaseDownloadResult:
        cfg = CaseDownloadConfig.from_raw(
            _section(context.config, "data.case_download")
        )

        if not cfg.enabled:
            context.log.info("download_case_data: disabled")
            return CaseDownloadResult(enabled=False)

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

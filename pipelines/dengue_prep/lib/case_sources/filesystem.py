"""Filesystem case source — wraps ``acestor.core.sources.FileSystemSource``.

Preserves prior behavior: reads raw case files from a local directory. The
``source_path`` or ``filesystem_base_path`` config key names the directory
(``source_path`` wins if both are set).
"""

from __future__ import annotations

from typing import Any, Mapping

from acestor.core.sources import FileSystemSource

from pipelines.dengue_prep.lib.case_sources import CaseSource


class Source(CaseSource):
    def __init__(self, base_path: str) -> None:
        self._impl = FileSystemSource(base_path=base_path)

    @classmethod
    def build(cls, config: Mapping[str, Any]) -> "Source":
        source_path = str(config.get("source_path", "")).strip()
        base_path = source_path or str(config.get("filesystem_base_path", "")).strip()
        if not base_path:
            raise ValueError(
                "case_sources.filesystem: no directory configured. "
                "Set data.case_download.source_path or filesystem_base_path in the pipeline config."
            )
        return cls(base_path=base_path)

    def list_objects(self, prefix: str = "") -> list[str]:
        return self._impl.list_objects(prefix)

    def read(self, path: str) -> bytes:
        return self._impl.read(path)

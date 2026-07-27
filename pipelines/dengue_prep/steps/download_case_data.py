from __future__ import annotations

import warnings
from typing import Any, ClassVar

from acestor import BaseStep, NoInputs, PipelineContext
from pipelines.dengue_prep.configs import PrepCaseDownloadConfig, _section
from pipelines.dengue_prep.lib.case_sources import load_source
from pipelines.dengue_prep.results import PrepCaseDownloadResult


# Legacy source_backend values map to their equivalent case-source plugin name.
# Anything else in source_backend is treated as-is (dotted module / file path).
_LEGACY_BACKEND_TO_MODE = {
    "filesystem": "filesystem",
    "s3": "s3",  # not yet ported — see issue #99
}


def _resolve_source_mode(cfg: PrepCaseDownloadConfig) -> str:
    """Resolve which source plugin to load.

    Preference order:
    1. Explicit ``source_mode`` in config.
    2. Legacy ``source_backend`` — emits DeprecationWarning if it's the only signal.
    3. Default to ``filesystem``.
    """
    if cfg.source_mode:
        return cfg.source_mode
    backend = (cfg.source_backend or "").strip().lower()
    if backend and backend not in _LEGACY_BACKEND_TO_MODE:
        # Unknown legacy value — try as-is (could be a dotted path someone set here).
        warnings.warn(
            "prep download_case_data: 'source_backend' is deprecated; use 'source_mode' "
            f"in data.case_download to select a case-source plugin. Passing "
            f"{backend!r} through as-is.",
            DeprecationWarning,
            stacklevel=3,
        )
        return backend
    if backend in _LEGACY_BACKEND_TO_MODE:
        warnings.warn(
            "prep download_case_data: 'source_backend' is deprecated; "
            "use 'source_mode' in data.case_download instead.",
            DeprecationWarning,
            stacklevel=3,
        )
        return _LEGACY_BACKEND_TO_MODE[backend]
    return "filesystem"


class PrepDownloadCaseDataStep(BaseStep[NoInputs, PrepCaseDownloadResult]):
    input_type: ClassVar[type] = NoInputs

    def __init__(self, source: Any | None = None) -> None:
        self.source = source

    def run(self, context: PipelineContext, inputs: NoInputs) -> PrepCaseDownloadResult:
        cfg = PrepCaseDownloadConfig.from_raw(
            _section(context.config, "data.case_download")
        )
        if not cfg.enabled:
            context.log.info("prep download_case_data: disabled")
            return PrepCaseDownloadResult(enabled=False)

        if self.source is not None:
            source = self.source
            source_mode = "injected"
        else:
            source_mode = _resolve_source_mode(cfg)
            # Forward the RAW case_download YAML section so plugin-specific
            # fields (e.g. dashboard's date_start / date_end / backfill_days /
            # base_url) reach the plugin's build() method. Also merge in the
            # typed-dataclass fields as a fallback so nothing depending on
            # them regresses.
            raw_download_section = dict(
                _section(context.config, "data.case_download") or {}
            )
            source_cfg = {**_dict_from_cfg(cfg), **raw_download_section}
            # Forward the parser's date_column candidates so content-scanning
            # sources (e.g. dashboard) know which columns to consult when
            # reading max-date out of existing xlsx files.
            parse_section = _section(context.config, "data.case_parse") or {}
            if parse_section.get("date_column"):
                source_cfg["date_column"] = parse_section["date_column"]
            try:
                source = load_source(source_mode, source_cfg)
            except ValueError as exc:
                # If plugin build fails and legacy source_storage is set, fall back
                # to the pipeline storage — preserves prior behaviour.
                if cfg.source_storage:
                    context.log.info(
                        "prep download_case_data: plugin %r not configured (%s) — "
                        "using storage %r",
                        source_mode,
                        exc,
                        cfg.source_storage,
                    )
                    source = context.require_storage(cfg.source_storage)
                    source_mode = f"storage:{cfg.source_storage}"
                else:
                    raise

        context.log.info(
            "prep download_case_data: source_mode=%s source_path=%r prefix=%r",
            source_mode,
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
                    f"(source_mode={source_mode})"
                ) from exc
            if not data:
                raise ValueError(
                    f"prep download_case_data: source file is empty: {src!r}"
                )
            copied.append(f"{source_mode}://{src}")

        context.log.info(
            "prep download_case_data: validated %d files (source_mode=%s)",
            len(copied),
            source_mode,
        )
        return PrepCaseDownloadResult(enabled=True, copied_files=copied)


def _dict_from_cfg(cfg: PrepCaseDownloadConfig) -> dict[str, Any]:
    """Flatten the config dataclass to a dict for the plugin's ``build`` method."""
    return {
        "source_path": cfg.source_path,
        "filesystem_base_path": cfg.filesystem_base_path,
        "s3_bucket": cfg.s3_bucket,
        "s3_prefix": cfg.s3_prefix,
        "cache_enabled": cfg.cache_enabled,
        "cache_dir": cfg.cache_dir,
        "cache_strategy": cfg.cache_strategy,
    }

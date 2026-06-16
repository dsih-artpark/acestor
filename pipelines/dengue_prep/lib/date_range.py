"""Single source of truth for the prep pipeline's date range.

Both parse_case_data and download_weather_data must operate over the same
time window — there's no sensible scenario where one starts at a different
date from the other. To prevent the two steps from drifting, they both read
through this helper instead of inlining their own merge of
``data.date_range.{start,end}`` into their typed configs.
"""

from __future__ import annotations

from typing import Any, Mapping


def resolve_date_range(config: Mapping[str, Any]) -> tuple[str, str]:
    """Return ``(start, end)`` strings from ``data.date_range``.

    Missing keys come back as empty strings. Sub-sections (case_parse,
    weather_download) may still override via their own ``date_start`` /
    ``start_date`` / ``date_end`` / ``end_date`` fields, but should rarely
    need to — keeping the two windows in sync is the default.
    """
    dr = ((config.get("data") or {}).get("date_range")) or {}
    return (
        str(dr.get("start", "")).strip(),
        str(dr.get("end", "")).strip(),
    )

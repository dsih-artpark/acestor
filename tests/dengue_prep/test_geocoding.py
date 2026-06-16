"""Tests for the geocoding resolver library.

Covers the pure-Python helpers (normalisation, fuzzy match, token extraction,
composition, cache) directly. The high-level ``resolve_via_geocode`` is
tested end-to-end with a mocked geolocator + a tmp geojson fixture — no real
Google calls.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest
from shapely.geometry import Polygon, mapping

from pipelines.dengue_prep.configs import (
    PrepCaseParseConfig,
    PrepGeocodingConfig,
)
from pipelines.dengue_prep.lib.geocoding import (
    DEFAULT_STOPWORDS,
    GeocodeCache,
    _lev_le1,
    compose_fields,
    extract_validation_tokens,
    formatted_address_contains_any,
    geocode_row_for_fields,
    normalize_address,
    resolve_via_geocode,
)


# ---------------------------------------------------------------------------
# normalize_address
# ---------------------------------------------------------------------------


def test_normalize_address_strips_hash_prefix():
    assert normalize_address("# 35, 1st Cross") == "35, 1st cross"


def test_normalize_address_collapses_whitespace():
    assert normalize_address("  502   03   Sri  Nilaya  ") == "502 03 sri nilaya"


def test_normalize_address_handles_nulls_as_empty():
    assert normalize_address(None) == ""
    assert normalize_address(float("nan")) == ""
    assert normalize_address("") == ""
    assert normalize_address("   ") == ""


# ---------------------------------------------------------------------------
# Levenshtein-distance-1 fuzzy matcher
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        # Identical
        ("hebbala", "hebbala", True),
        # 1-char insertion
        ("hebbal", "hebbala", True),
        ("vimanpura", "vimanapura", True),
        ("bellandur", "bellanduru", True),
        ("chickpet", "chickpete", True),
        ("nagarbhavi", "nagarabhavi", True),
        ("indranagar", "indiranagar", True),
        ("attur", "atturu", True),
        # 1-char substitution
        ("abcd", "abxd", True),
        # 2+ char diff — rejected
        ("halsoor", "halasuru", False),
        ("foo", "barbaz", False),
        ("abcd", "abxy", False),
        # length diff > 1 — rejected
        ("a", "abc", False),
    ],
)
def test_lev_le1(a: str, b: str, expected: bool):
    assert _lev_le1(a, b) is expected


def test_lev_le1_is_symmetric():
    # Both orderings should agree.
    assert _lev_le1("hebbal", "hebbala") == _lev_le1("hebbala", "hebbal")
    assert _lev_le1("halsoor", "halasuru") == _lev_le1("halasuru", "halsoor")


# ---------------------------------------------------------------------------
# formatted_address_contains_any
# ---------------------------------------------------------------------------


def test_contains_any_exact_substring():
    """Pure substring match — current fast path."""
    fmt = "Domlur, Bengaluru, Karnataka, India"
    assert formatted_address_contains_any(fmt, {"domlur"}) is True


def test_contains_any_case_insensitive_on_formatted():
    """Token contract is lowercase; formatted side may be mixed-case."""
    fmt = "DOMLUR, BENGALURU"
    assert formatted_address_contains_any(fmt, {"domlur"}) is True


def test_contains_any_fuzzy_recovers_spelling():
    """The whole point of the fuzzy layer — 1-char spelling diffs validate."""
    fmt = "Hebbal, Bengaluru, Karnataka, India"
    assert formatted_address_contains_any(fmt, {"hebbala"}) is True

    fmt2 = "Vimanapura, Bengaluru"
    assert formatted_address_contains_any(fmt2, {"vimanpura"}) is True


def test_contains_any_fuzzy_does_not_validate_unrelated_areas():
    """Murugeshpalya bug guard: token from one area must not match a different area."""
    fmt = "Murugeshpalya, Bengaluru, Karnataka 560017, India"
    assert formatted_address_contains_any(fmt, {"domlur", "layout"}) is False


def test_contains_any_rejects_jhansi_for_hebbala():
    """Empirical: Jhansi UP must not validate as Hebbal Bengaluru even with fuzzy."""
    fmt = "Jhansi, Uttar Pradesh, India"
    assert formatted_address_contains_any(fmt, {"hebbala"}) is False


def test_contains_any_empty_tokens_returns_false():
    """No tokens → no signal → can't trust."""
    assert formatted_address_contains_any("Anywhere, India", set()) is False


def test_contains_any_null_formatted_returns_false():
    assert formatted_address_contains_any(None, {"hebbala"}) is False
    assert formatted_address_contains_any(float("nan"), {"hebbala"}) is False


# ---------------------------------------------------------------------------
# extract_validation_tokens
# ---------------------------------------------------------------------------


def test_extract_tokens_strips_ward_no_prefix():
    """`Ward No.21(Hebbala)` → token `{hebbala}` (the parens prefix gets dropped)."""
    row = pd.Series({"Village Or Ward": "Ward No.21(Hebbala)"})
    tokens = extract_validation_tokens(row, ["Village Or Ward"])
    assert tokens == {"hebbala"}


def test_extract_tokens_filters_stopwords():
    """Generic Bengaluru/Karnataka/Corporation tokens must not survive."""
    row = pd.Series({"Ulb": "Greater Bengaluru Authority", "State": "Karnataka"})
    tokens = extract_validation_tokens(row, ["Ulb", "State"])
    # Every word is in the default stopwords; nothing left.
    assert tokens == set()


def test_extract_tokens_keeps_specific_area_names():
    row = pd.Series(
        {
            "Village Or Ward": "Domlur Layout",
            "Sub District": "Bengaluru South",
            "Ulb": "Greater Bengaluru Authority",
        }
    )
    tokens = extract_validation_tokens(row, ["Village Or Ward", "Sub District", "Ulb"])
    # domlur + layout from V/W; south from SubD; Ulb fully stopworded.
    assert "domlur" in tokens
    assert "layout" in tokens
    assert "south" in tokens
    assert "greater" not in tokens
    assert "authority" not in tokens
    assert "karnataka" not in tokens


def test_extract_tokens_handles_missing_columns_gracefully():
    row = pd.Series({"a": "Domlur Layout"})
    tokens = extract_validation_tokens(row, ["a", "does_not_exist"])
    assert tokens == {"domlur", "layout"}


def test_extract_tokens_skips_short_words():
    """Default min_len=4 — discards 'in', 'a', 'no', etc."""
    row = pd.Series({"x": "in a no out"})
    tokens = extract_validation_tokens(row, ["x"])
    assert tokens == set()  # all <4 chars


def test_extract_tokens_respects_extra_stopwords():
    row = pd.Series({"V": "Bommenahalli"})
    custom = frozenset(DEFAULT_STOPWORDS | {"bommenahalli"})
    tokens = extract_validation_tokens(row, ["V"], stopwords=custom)
    assert tokens == set()


# ---------------------------------------------------------------------------
# compose_fields
# ---------------------------------------------------------------------------


def test_compose_joins_fields_in_order():
    row = pd.Series(
        {
            "Patient Address": "502 Sri Nilaya",
            "Village Or Ward": "Domlur Layout",
            "Ulb": "BBMP",
        }
    )
    out = compose_fields(row, ["Patient Address", "Village Or Ward", "Ulb"])
    assert out == "502 Sri Nilaya, Domlur Layout, BBMP"


def test_compose_skips_empty_and_null_fields():
    row = pd.Series({"a": "First", "b": None, "c": "   ", "d": "Last"})
    assert compose_fields(row, ["a", "b", "c", "d"]) == "First, Last"


def test_compose_skips_missing_columns():
    row = pd.Series({"a": "Only"})
    assert compose_fields(row, ["a", "does_not_exist"]) == "Only"


def test_compose_returns_empty_when_no_data():
    row = pd.Series({"a": None, "b": ""})
    assert compose_fields(row, ["a", "b", "missing"]) == ""


# ---------------------------------------------------------------------------
# GeocodeCache
# ---------------------------------------------------------------------------


def test_cache_round_trip(tmp_path: Path):
    p = tmp_path / "cache.json"
    cache = GeocodeCache(p)
    assert cache.get("anything") is None
    cache.put("hello", {"formatted_address": "Hello, World"})
    cache.flush()

    cache2 = GeocodeCache(p)
    assert cache2.get("hello") == {"formatted_address": "Hello, World"}


def test_cache_handles_malformed_file(tmp_path: Path):
    p = tmp_path / "bad.json"
    p.write_text("{not valid json}")
    cache = GeocodeCache(p)
    # Should not raise; starts fresh.
    assert cache.get("anything") is None


def test_cache_summary_reports_hit_miss():
    cache = GeocodeCache(Path("/tmp/__nonexistent_cache__.json"))
    cache.put("k", {"v": 1})
    assert cache.get("k") is not None
    assert cache.get("missing") is None
    s = cache.summary()
    assert "1 hits" in s
    assert "2 lookups" in s


# ---------------------------------------------------------------------------
# Mocked geolocator + geocode_row_for_fields
# ---------------------------------------------------------------------------


class _FakeLocation:
    def __init__(self, address: str, lat: float, lon: float, kind: str = "ROOFTOP"):
        self.address = address
        self.latitude = lat
        self.longitude = lon
        self.raw = {"geometry": {"location_type": kind}}


class _FakeGeolocator:
    """Stubbed geopy-compatible geolocator. Returns canned answers per address."""

    def __init__(self, responses: dict[str, _FakeLocation | None]):
        self.responses = responses
        self.calls: list[str] = []
        self.bounds_calls: list[tuple | None] = []

    def geocode(self, address: str, **kwargs):
        self.calls.append(address)
        self.bounds_calls.append(kwargs.get("bounds"))
        # Match by lowercased substring so the test can index by short keys.
        for key, resp in self.responses.items():
            if key.lower() in address.lower():
                return resp
        return None


def test_geocode_row_validates_against_token(tmp_path: Path):
    row = pd.Series(
        {
            "Patient Address": "10 MG Road, Domlur",
            "Village Or Ward": "Domlur Layout",
        }
    )
    geolocator = _FakeGeolocator(
        {"domlur": _FakeLocation("Domlur, Bengaluru, Karnataka, India", 12.96, 77.64)}
    )
    cache = GeocodeCache(tmp_path / "cache.json")
    result, calls = geocode_row_for_fields(
        row, ["Patient Address", "Village Or Ward"], cache, geolocator
    )
    assert result.validated is True
    assert result.lat == 12.96
    assert result.long == 77.64
    assert calls == 1  # cache was empty, made one API call


def test_geocode_row_rejects_when_returned_area_does_not_match(tmp_path: Path):
    row = pd.Series(
        {
            "Patient Address": "Sri Nilaya",
            "Village Or Ward": "Domlur Layout",
        }
    )
    # Google returns a Murugeshpalya address, no overlap with 'domlur' / 'layout'.
    geolocator = _FakeGeolocator(
        {
            "sri nilaya": _FakeLocation(
                "Murugeshpalya, Bengaluru, Karnataka 560017", 12.95, 77.65
            )
        }
    )
    cache = GeocodeCache(tmp_path / "cache.json")
    result, _ = geocode_row_for_fields(
        row, ["Patient Address", "Village Or Ward"], cache, geolocator
    )
    assert result.validated is False
    assert result.lat is None  # rejected — coords not surfaced
    assert result.formatted_address  # we still keep what Google said (for audit)


def test_geocode_row_fuzzy_validates_one_char_spelling(tmp_path: Path):
    """Source says 'Hebbala'; Google says 'Hebbal' — fuzzy must accept."""
    row = pd.Series(
        {"Patient Address": "near canara bank hebbal", "Village Or Ward": "Hebbala"}
    )
    geolocator = _FakeGeolocator(
        {"hebbal": _FakeLocation("Hebbal, Bengaluru, Karnataka, India", 13.04, 77.59)}
    )
    cache = GeocodeCache(tmp_path / "cache.json")
    result, _ = geocode_row_for_fields(
        row, ["Patient Address", "Village Or Ward"], cache, geolocator
    )
    assert result.validated is True
    assert result.lat == 13.04


def test_geocode_row_cache_hit_skips_api_call(tmp_path: Path):
    """Second call with same address must not hit Google again."""
    row = pd.Series({"Patient Address": "Domlur Layout"})
    geolocator = _FakeGeolocator(
        {"domlur": _FakeLocation("Domlur, Bengaluru, Karnataka", 12.96, 77.64)}
    )
    cache = GeocodeCache(tmp_path / "cache.json")
    _, calls1 = geocode_row_for_fields(row, ["Patient Address"], cache, geolocator)
    _, calls2 = geocode_row_for_fields(row, ["Patient Address"], cache, geolocator)
    assert calls1 == 1
    assert calls2 == 0  # cache hit


def test_geocode_row_empty_address_skips_geocoding(tmp_path: Path):
    row = pd.Series({"Patient Address": None})
    geolocator = _FakeGeolocator({})
    cache = GeocodeCache(tmp_path / "cache.json")
    result, calls = geocode_row_for_fields(row, ["Patient Address"], cache, geolocator)
    assert result.attempted is False
    assert calls == 0


# ---------------------------------------------------------------------------
# resolve_via_geocode end-to-end (mocked geolocator + tmp geojson)
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_ward_layer(tmp_path: Path) -> Path:
    """Two tiny ward polygons covering disjoint squares around Bengaluru-ish coords."""
    d = tmp_path / "wards"
    d.mkdir()

    # Ward A: covers (12.95–12.97, 77.60–77.62)
    poly_a = Polygon([(77.60, 12.95), (77.62, 12.95), (77.62, 12.97), (77.60, 12.97)])
    (d / "ward_a.geojson").write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"region_id": "ward_A", "name": "WardA"},
                        "geometry": mapping(poly_a),
                    }
                ],
            }
        )
    )

    # Ward B: covers (13.03–13.05, 77.58–77.60)
    poly_b = Polygon([(77.58, 13.03), (77.60, 13.03), (77.60, 13.05), (77.58, 13.05)])
    (d / "ward_b.geojson").write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"region_id": "ward_B", "name": "WardB"},
                        "geometry": mapping(poly_b),
                    }
                ],
            }
        )
    )

    return d


def test_resolve_via_geocode_two_pass_fallback_recovers(
    tmp_path: Path, tmp_ward_layer: Path
):
    """The headline test: primary fails (cross-state), fallback recovers via Facility."""
    df = pd.DataFrame(
        [
            # Row 0: primary works — patient in WardA area.
            {
                "Patient Address": "in WardA",
                "Village Or Ward": "WardA",
                "Facility Name Lform": "Hospital In WardA",
            },
            # Row 1: cross-state — primary returns Jhansi (outside the layer),
            # fallback uses Facility Name (in WardB) and recovers.
            {
                "Patient Address": "Jhansi, UP",
                "Village Or Ward": "WardB",
                "Facility Name Lform": "Hospital In WardB",
            },
        ]
    )
    geolocator = _FakeGeolocator(
        {
            # Primary attempts
            "in WardA": _FakeLocation("WardA, Bengaluru, Karnataka", 12.96, 77.61),
            "Jhansi, UP": _FakeLocation("Jhansi, Uttar Pradesh, India", 25.45, 78.57),
            # Fallback attempts (Facility composition for row 1)
            "Hospital In WardB": _FakeLocation(
                "Hospital In WardB, Bengaluru, Karnataka", 13.04, 77.59
            ),
        }
    )

    # Patch make_geolocator so resolve_via_geocode picks up our fake.
    with patch(
        "pipelines.dengue_prep.lib.geocoding.make_geolocator",
        return_value=geolocator,
    ):
        region_ids = resolve_via_geocode(
            df,
            address_fields=["Patient Address", "Village Or Ward"],
            fallback_address_fields=["Facility Name Lform", "Village Or Ward"],
            cache_file=tmp_path / "cache.json",
            geojson_dir=tmp_ward_layer,
        )

    assert region_ids.tolist() == ["ward_A", "ward_B"]


def test_resolve_via_geocode_without_fallback_drops_jhansi(
    tmp_path: Path, tmp_ward_layer: Path
):
    """When fallback is empty, the Jhansi row should land as NaN (no recovery)."""
    df = pd.DataFrame(
        [
            {"Patient Address": "in WardA", "Village Or Ward": "WardA"},
            {"Patient Address": "Jhansi, UP", "Village Or Ward": "WardB"},
        ]
    )
    geolocator = _FakeGeolocator(
        {
            "in WardA": _FakeLocation("WardA, Bengaluru, Karnataka", 12.96, 77.61),
            "Jhansi, UP": _FakeLocation("Jhansi, Uttar Pradesh, India", 25.45, 78.57),
        }
    )

    with patch(
        "pipelines.dengue_prep.lib.geocoding.make_geolocator",
        return_value=geolocator,
    ):
        region_ids = resolve_via_geocode(
            df,
            address_fields=["Patient Address", "Village Or Ward"],
            cache_file=tmp_path / "cache.json",
            geojson_dir=tmp_ward_layer,
        )

    assert region_ids.iloc[0] == "ward_A"
    assert pd.isna(region_ids.iloc[1])


def test_resolve_via_geocode_raises_on_unknown_address_fields(
    tmp_path: Path, tmp_ward_layer: Path
):
    df = pd.DataFrame([{"Some Other Column": "x"}])
    with pytest.raises(ValueError, match="address_fields not found"):
        resolve_via_geocode(
            df,
            address_fields=["Does Not Exist"],
            cache_file=tmp_path / "c.json",
            geojson_dir=tmp_ward_layer,
        )


def test_resolve_via_geocode_raises_on_empty_address_fields(
    tmp_path: Path, tmp_ward_layer: Path
):
    df = pd.DataFrame([{"a": "x"}])
    with pytest.raises(ValueError, match="at least one"):
        resolve_via_geocode(
            df,
            address_fields=[],
            cache_file=tmp_path / "c.json",
            geojson_dir=tmp_ward_layer,
        )


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


def test_geocoding_config_default_disabled():
    cfg = PrepGeocodingConfig.from_raw(None)
    assert cfg.enabled is False
    assert cfg.address_fields == ()
    assert cfg.fallback_address_fields == ()


def test_geocoding_config_parses_arrays():
    cfg = PrepGeocodingConfig.from_raw(
        {
            "enabled": True,
            "address_fields": ["Patient Address", "Village Or Ward"],
            "fallback_address_fields": ["Facility Name Lform"],
            "extra_stopwords": ["bommenahalli"],
            "cache_file": "custom.json",
        }
    )
    assert cfg.enabled is True
    assert cfg.address_fields == ("Patient Address", "Village Or Ward")
    assert cfg.fallback_address_fields == ("Facility Name Lform",)
    assert cfg.extra_stopwords == ("bommenahalli",)
    assert cfg.cache_file == "custom.json"


def test_geocoding_config_rejects_non_list_fields():
    with pytest.raises(ValueError, match="address_fields"):
        PrepGeocodingConfig.from_raw({"address_fields": "Patient Address"})


def test_case_parse_rejects_enabled_geocoding_without_fields():
    """Sanity guard: turning geocoding on with no fields is operator error."""
    with pytest.raises(ValueError, match="address_fields"):
        PrepCaseParseConfig.from_raw(
            {
                "region_types": ["ward"],
                "geocoding": {"enabled": True},
            }
        )


# ---------------------------------------------------------------------------
# Bounds-biased geocoding (production hardening)
# ---------------------------------------------------------------------------


def test_geocoding_config_parses_bounds():
    cfg = PrepGeocodingConfig.from_raw(
        {
            "enabled": True,
            "address_fields": ["Patient Address"],
            "bounds": [12.5, 77.3, 13.3, 77.9],
        }
    )
    assert cfg.bounds == (12.5, 77.3, 13.3, 77.9)


def test_geocoding_config_rejects_malformed_bounds():
    with pytest.raises(ValueError, match="bounds"):
        PrepGeocodingConfig.from_raw(
            {"address_fields": ["x"], "bounds": [12.5, 77.3, 13.3]}
        )


def test_geocoding_config_bounds_default_is_none():
    cfg = PrepGeocodingConfig.from_raw({"address_fields": ["x"]})
    assert cfg.bounds is None


def test_geocode_row_passes_bounds_to_geolocator(tmp_path: Path):
    row = pd.Series({"Patient Address": "Domlur"})
    geolocator = _FakeGeolocator(
        {"domlur": _FakeLocation("Domlur, Bengaluru, Karnataka", 12.96, 77.64)}
    )
    cache = GeocodeCache(tmp_path / "cache.json")
    bounds = (12.5, 77.3, 13.3, 77.9)
    geocode_row_for_fields(row, ["Patient Address"], cache, geolocator, bounds=bounds)
    assert geolocator.bounds_calls == [[(12.5, 77.3), (13.3, 77.9)]]


def test_geocode_cache_segregates_bounded_and_unbounded(tmp_path: Path):
    """Same address with different bounds must not collide in the cache."""
    row = pd.Series({"Patient Address": "Domlur"})
    geolocator = _FakeGeolocator(
        {"domlur": _FakeLocation("Domlur, Bengaluru, Karnataka", 12.96, 77.64)}
    )
    cache = GeocodeCache(tmp_path / "cache.json")
    geocode_row_for_fields(row, ["Patient Address"], cache, geolocator)
    geocode_row_for_fields(
        row,
        ["Patient Address"],
        cache,
        geolocator,
        bounds=(12.5, 77.3, 13.3, 77.9),
    )
    # Two distinct cache entries → two real geolocator calls.
    assert len(geolocator.calls) == 2


# ---------------------------------------------------------------------------
# Drop-if-cross-state guardrail (production hardening)
# ---------------------------------------------------------------------------


def test_geocoding_config_parses_restrict_admin_area():
    cfg = PrepGeocodingConfig.from_raw(
        {
            "enabled": True,
            "address_fields": ["x"],
            "restrict_admin_area_tokens": ["Karnataka"],
        }
    )
    assert cfg.restrict_admin_area_tokens == ("Karnataka",)


def test_geocoding_config_restrict_default_is_empty():
    cfg = PrepGeocodingConfig.from_raw({"address_fields": ["x"]})
    assert cfg.restrict_admin_area_tokens == ()


def test_restrict_admin_area_rejects_cross_state_hit(tmp_path: Path):
    """Patient Address geocoded to Jhansi UP must be rejected when Karnataka is required."""
    row = pd.Series({"Patient Address": "jhansi, uttar pradesh"})
    geolocator = _FakeGeolocator(
        {"jhansi": _FakeLocation("Jhansi, Uttar Pradesh, India", 25.45, 78.57)}
    )
    cache = GeocodeCache(tmp_path / "cache.json")
    result, _ = geocode_row_for_fields(
        row,
        ["Patient Address"],
        cache,
        geolocator,
        restrict_admin_area_tokens=("Karnataka",),
    )
    # Token validation passes ("jhansi" appears in formatted_address) but the
    # cross-state guardrail rejects it.
    assert result.validated is False
    assert result.lat is None


def test_restrict_admin_area_accepts_in_state_hit(tmp_path: Path):
    row = pd.Series({"Patient Address": "Domlur"})
    geolocator = _FakeGeolocator(
        {"domlur": _FakeLocation("Domlur, Bengaluru, Karnataka, India", 12.96, 77.64)}
    )
    cache = GeocodeCache(tmp_path / "cache.json")
    result, _ = geocode_row_for_fields(
        row,
        ["Patient Address"],
        cache,
        geolocator,
        restrict_admin_area_tokens=("Karnataka",),
    )
    assert result.validated is True
    assert result.lat == 12.96


def test_restrict_admin_area_empty_is_no_op(tmp_path: Path):
    """Empty tuple must not silently reject — it means 'no restriction'."""
    row = pd.Series({"Patient Address": "Domlur"})
    geolocator = _FakeGeolocator(
        {"domlur": _FakeLocation("Domlur, Bengaluru, Karnataka", 12.96, 77.64)}
    )
    cache = GeocodeCache(tmp_path / "cache.json")
    result, _ = geocode_row_for_fields(
        row, ["Patient Address"], cache, geolocator, restrict_admin_area_tokens=()
    )
    assert result.validated is True

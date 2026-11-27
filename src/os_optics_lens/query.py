"""Query operations over an opened index (spec §6.5-§6.7, §7; amendments 10.3, 10.7, 10.9-10.16).

Operations (each takes an opened `store.Index` and returns a complete response envelope
`{"ok": true, "snapshot", "result" | "results", "warnings"?, "bounds"}` whose exact wire size is
accounted for by `output`):

* `facets` - facet values with record counts within a filtered scope.
* `fields` - typed fact keys with units, bases, condition variants and ranges; `fact=KEY` describes
  one key with its applicable operators and a ready-to-run example constraint.
* `search` / `run_request` - specification and starting-point search, single or batch, with
  deterministic ordering, budgeted pages and continuation cursors.
* `compare` - a short list of records/configurations side by side, flagging incomparable rows.
* `inspect` - one record's identity, configurations, available facts and supporting calculations,
  with selected facts, evidence, prescription and direct provenance on request.
* `project_metadata` / `project_bytes` - the native Project hand-off (the CLI performs file I/O).

Every public entry point raises `errors.LensError` with the codes of spec §7. Constraint semantics
come from `evaluate`; this module validates requests against a closed schema (unknown keys are
rejected with a JSON pointer and `did_you_mean`), resolves vocabulary aliases, applies discovery
filters, evaluates each candidate configuration and orders, pages and summarizes the results.
"""

from __future__ import annotations

import copy
import difflib
import json
import math
import re
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Sequence

from . import output
from .canonical import canonical_key, corpus_relative_path
from .errors import LensError
from .evaluate import (
    CONDITIONS_DIFFER,
    DOES_NOT_MATCH,
    MATCH,
    OUTCOMES,
    SAMPLING_UNSPECIFIED,
    UNCONFIRMED,
    EvalContext,
    RequestError,
    configuration_rank,
    constraint_fact_key,
    evaluate_configuration,
    expand_constraint,
    fact_present,
    is_fixture_preference,
    is_shorthand,
    json_equal,
    preference_rank_entry,
    preference_value,
    public_fact_value,
    ranking_conditions_equal,
    target_deviation,
    target_value,
    validate_preferences,
    validate_target,
)
from .normalize import iter_source_refs
from .project_facts import analysis_applies
from .store import NOTICE_FIELD, Index
from .vocabulary import Vocabulary, load_vocabulary

__all__ = [
    "MODES",
    "facets",
    "fields",
    "search",
    "run_request",
    "explain_request",
    "normalize_request",
    "compare",
    "inspect",
    "project_metadata",
    "project_bytes",
    "project_response",
    "fixture_to_request",
]

MODES = ("specification", "starting_point")
SPECIFICATION, STARTING_POINT = MODES
DEFAULT_LIMIT = 10
MAX_LIMIT = 50
MAX_BATCH_QUERIES = 20
MAX_SUMMARY_FIELDS = 12
MAX_COMPARE_REFS = 10
MAX_COMPARE_FIELDS = 40
DEFAULT_FACET_LIMIT = 20
MAX_FACET_LIMIT = 200
DEFAULT_FIELDS_LIMIT = 100
MAX_FIELDS_LIMIT = 500
MAX_VARIANTS = 10
EVALUATION_SCOPES = ("one_nominal_configuration",)
HARD_SHORTFALL = ("exclude", "allow")
DETAILS = ("compact", "full")
PRESCRIPTION_LEVELS = ("summary", "full")

_QUERY_KEYS = frozenset({
    "mode", "filters", "constraints", "targets", "preferences", "include", "fields", "detail", "limit",
    "cursor", "evaluation_scope", "hard_shortfall", "max_output_bytes",
})
_SUB_QUERY_KEYS = _QUERY_KEYS - {"max_output_bytes"}
_BATCH_KEYS = frozenset({"queries", "max_output_bytes"})
_FILTER_KEYS = frozenset({"facets", "ranges", "spectral_span", "record_ids", "collections"})
_RANGE_KEYS = frozenset({"fact", "min", "max"})

_EXCLUDED_STAGES = ("filters", "missing_fact", "out_of_range")
_VALUE_MEMBER = re.compile(r"^value(_[a-z_]+)?$")
_INDEX_SEGMENT = re.compile(r"^([^\[\]]+)((?:\[\d+\])*)$")
_ENTRY_REF_FIELD = re.compile(r"^configurations\[(\d+)\]\.")
_LICENSE_PATH = re.compile(r"\b(licenses/[A-Za-z0-9._-]+|LICENSE)\b")
_PROVENANCE_MEMBERS = ("source", "source_url", "source_revision", "source_input", "source_input_sha256",
                       "source_links", "original_publication", "contributor", "attribution")

_FIELD_CANDIDATES = ("field.half_field_deg", "field.max_image_height_mm", "field.max_object_height_mm")
_DEFAULT_LEADING = ("first_order.effective_focal_length_mm",)
_DEFAULT_TRAILING = ("image_format.image_circle_diameter_mm", "first_order.total_track_mm",
                     "spectrum.sampling_span_nm")
_PARAXIAL_LENGTHS = frozenset({"first_order.effective_focal_length_mm", "first_order.back_focal_length_mm"})

_OPS_BY_TYPE = {
    "number": ["eq", "ne", "gt", "gte", "lt", "lte", "between_inclusive", "in"],
    "interval": ["covers_interval", "gt", "gte", "lt", "lte", "between_inclusive"],
    "list": ["includes_all", "eq", "ne", "in"],
    "string": ["eq", "ne", "in"],
    "bool": ["eq", "ne"],
    "object": ["has_source", "has_sourced_method", "eq", "ne"],
    "none": ["gt", "gte", "lt", "lte", "between_inclusive", "eq", "ne"],
}


# ---------------------------------------------------------------------------------------------
# Small helpers


def _ptr(base: str, *parts: object) -> str:
    """A JSON pointer: `base` extended by escaped reference tokens."""
    return base + "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def _invalid(message: str, pointer: str, hint: str | None = None, **details: Any) -> LensError:
    return LensError("INVALID_REQUEST", message, hint=hint, details={"pointer": pointer, **details})


@contextmanager
def _request_errors() -> Iterator[None]:
    """Translate evaluator request errors into `LensError` with the same code and details."""
    try:
        yield
    except RequestError as error:
        raise LensError(error.code, error.message, hint=error.hint, details=dict(error.details)) from None


def _check_keys(raw: dict[str, Any], allowed: frozenset[str], pointer: str, what: str) -> None:
    """Closed schema: every key of `raw` must be in `allowed` (amendment 10.10)."""
    for key in sorted(raw, key=str):
        if key not in allowed:
            close = difflib.get_close_matches(str(key), sorted(allowed), n=3, cutoff=0.6)
            hint = f"did you mean {', '.join(close)}?" if close else f"allowed keys: {', '.join(sorted(allowed))}"
            raise _invalid(f"unknown key {_short_text(str(key))!r} in {what}", _ptr(pointer, key), hint,
                           did_you_mean=close, allowed=sorted(allowed))


def _is_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # an int too large for a double
        return False


def _check_int(value: Any, pointer: str, name: str, low: int, high: int, default: int) -> int:
    """An integer option in [low, high]; out-of-range values are rejected, never clamped."""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise _invalid(f"{name} must be an integer from {low} to {high}", pointer,
                       actual=value, allowed={"min": low, "max": high})
    return value


def _string_list(value: Any, pointer: str, name: str, *, max_items: int | None = None) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not value or not all(isinstance(v, str) and v for v in value):
        raise _invalid(f"{name} must be a non-empty list of strings", pointer)
    if max_items is not None and len(value) > max_items:
        raise _invalid(f"{name} takes at most {max_items} entries", pointer,
                       actual=len(value), allowed={"max": max_items})
    return list(value)


def _short_text(text: Any, limit: int = 160) -> Any:
    if isinstance(text, str) and len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def _at_path(data: Any, json_path: str) -> Any:
    """The value at a dotted path with `[i]` list indices (`performance[2].value`), or None."""
    if not json_path:
        return data
    for segment in json_path.split("."):
        match = _INDEX_SEGMENT.match(segment)
        if match is None:
            return None
        name, indices = match.group(1), match.group(2)
        data = data.get(name) if isinstance(data, dict) else None
        for index in re.findall(r"\[(\d+)\]", indices):
            position = int(index)
            data = data[position] if isinstance(data, list) and position < len(data) else None
    return data


# ---------------------------------------------------------------------------------------------
# Session: per-call caches over one opened index


class _Session:
    """Caches index lookups for the duration of one public call."""

    def __init__(self, index: Index) -> None:
        if not isinstance(index, Index):
            raise LensError("INTERNAL", "query functions need an opened os_optics_lens.store.Index")
        self.index = index
        self.vocab: Vocabulary = load_vocabulary()
        self.ctx = EvalContext(
            stop_dependent=self.vocab.stop_dependent_facts,
            stop_independent=self.vocab.stop_independent,
            stop_dependent_sections=self.vocab.stop_dependent_sections,
            resolve_source=index.resolve_source,
        )
        self.snapshot = output.snapshot_info(index)
        self._fact_keys: list[str] | None = None
        self._fact_key_set: frozenset[str] | None = None
        self._numeric_keys: frozenset[str] | None = None
        self._records: dict[int, dict[str, Any]] | None = None
        self._record_by_id: dict[str, dict[str, Any]] | None = None
        self._configs: list[dict[str, Any]] | None = None
        self._record_facets: dict[int, dict[str, set[str]]] | None = None
        self._config_facets: dict[int, dict[str, set[str]]] | None = None
        self._facet_values: dict[str, set[str]] = {}
        self._views: dict[int, dict[str, Any]] = {}
        self._facts: dict[int, dict[str, list[dict[str, Any]]]] = {}
        self._key_rows: dict[str, list[dict[str, Any]]] = {}
        self._all_views_loaded = False

    # Fact keys ----------------------------------------------------------------------------

    def fact_keys(self) -> list[str]:
        if self._fact_keys is None:
            self._fact_keys = self.index.fact_keys()
            self._fact_key_set = frozenset(self._fact_keys)
        return self._fact_keys

    def is_known_fact(self, key: str) -> bool:
        """A fact key present in the index, or a path inside one (`<key>.<member>`)."""
        self.fact_keys()
        assert self._fact_key_set is not None
        if key in self._fact_key_set:
            return True
        segments = key.split(".")
        return any(".".join(segments[:end]) in self._fact_key_set for end in range(1, len(segments)))

    def known_prefix(self, key: str) -> str | None:
        """The longest indexed fact key equal to `key` or a dotted prefix of it."""
        self.fact_keys()
        assert self._fact_key_set is not None
        segments = key.split(".")
        for end in range(len(segments), 0, -1):
            candidate = ".".join(segments[:end])
            if candidate in self._fact_key_set:
                return candidate
        return None

    def did_you_mean(self, key: str, limit: int = 3) -> list[str]:
        keys = self.fact_keys()
        close = difflib.get_close_matches(key, keys, n=limit, cutoff=0.5)
        leaf = key.split(".")[-1]
        for candidate in keys:
            if len(close) >= limit:
                break
            if candidate.split(".")[-1] == leaf and candidate not in close:
                close.append(candidate)
        return close[:limit]

    def unknown_field(self, key: Any, pointer: str) -> LensError:
        close = self.did_you_mean(key) if isinstance(key, str) else []
        return LensError(
            "UNKNOWN_FIELD", f"fact key {key!r} is not in this index",
            hint=("did you mean " + ", ".join(close) + "?") if close else "list fact keys with `os-optics-lens fields`",
            details={"pointer": pointer, "fact": key, "did_you_mean": close})

    def reject_check(self, key: Any, pointer: str) -> None:
        """A name (fact key or predicate path) that reaches a provenance check is an error: checks
        compare source data and can be neither matched nor ranked."""
        if not isinstance(key, str):
            return
        members = self.vocab.fact_extraction.get("check_members", [])
        member = next((segment for segment in key.split(".") if segment in members), None)
        if member is None:
            return
        raise LensError(
            "UNKNOWN_FIELD", f"{_short_text(key)!r} names {member}, a provenance check, not a fact",
            hint=f"{member} is a provenance check that cannot be matched or ranked. "
                 "Use `os-optics-lens inspect <record_id> --provenance` for the direct source and terms.",
            details={"pointer": pointer, "fact": _short_text(key), "provenance_check": member,
                     "did_you_mean": []})

    def require_fact(self, key: Any, pointer: str) -> str:
        if not isinstance(key, str) or not key:
            raise _invalid("fact must be a dotted fact key", pointer)
        self.reject_check(key, pointer)
        if not self.is_known_fact(key):
            raise self.unknown_field(key, pointer)
        return key

    def key_rows(self, key: str) -> list[dict[str, Any]]:
        if key not in self._key_rows:
            self._key_rows[key] = self.index.facts(fact_key=key)
        return self._key_rows[key]

    # Records, configurations, facets --------------------------------------------------------

    def records(self) -> dict[int, dict[str, Any]]:
        if self._records is None:
            self._records = {row["record_pk"]: row for row in self.index.iter_records()}
            self._record_by_id = {row["record_id"]: row for row in self._records.values()}
        return self._records

    def record_by_id(self, record_id: str) -> dict[str, Any] | None:
        self.records()
        assert self._record_by_id is not None
        return self._record_by_id.get(record_id)

    def configs(self) -> list[dict[str, Any]]:
        """Every configuration (no views), ordered by record_id then ordinal."""
        if self._configs is None:
            self._configs = list(self.index.iter_configurations(with_view=False))
        return self._configs

    def _load_facets(self) -> None:
        if self._record_facets is not None:
            return
        record_facets: dict[int, dict[str, set[str]]] = {}
        config_facets: dict[int, dict[str, set[str]]] = {}
        for row in self.index.facet_rows():
            self._facet_values.setdefault(row["facet"], set()).add(row["value"])
            if row["config_pk"] is None:
                record_facets.setdefault(row["record_pk"], {}).setdefault(row["facet"], set()).add(row["value"])
            else:
                config_facets.setdefault(row["config_pk"], {}).setdefault(row["facet"], set()).add(row["value"])
        self._record_facets, self._config_facets = record_facets, config_facets

    def facet_values(self, facet: str) -> set[str]:
        self._load_facets()
        return self._facet_values.get(facet, set())

    def facets_of(self, config: dict[str, Any], facet: str) -> set[str]:
        """Values of a facet for one configuration (record-level and configuration-level rows)."""
        self._load_facets()
        assert self._record_facets is not None and self._config_facets is not None
        values = set(self._record_facets.get(config["record_pk"], {}).get(facet, ()))
        values |= self._config_facets.get(config["config_pk"], {}).get(facet, set())
        return values

    # Views and facts ------------------------------------------------------------------------

    def view(self, config: dict[str, Any]) -> dict[str, Any]:
        pk = config["config_pk"]
        if pk not in self._views:
            for item in self.index.configurations(config["record_pk"], with_view=True):
                self._views[item["config_pk"]] = item["view"]
        return self._views[pk]

    def all_views(self) -> list[dict[str, Any]]:
        if not self._all_views_loaded:
            for item in self.index.iter_configurations(with_view=True):
                self._views.setdefault(item["config_pk"], item["view"])
            self._all_views_loaded = True
        return [self._views[c["config_pk"]] for c in self.configs()]

    def config_facts(self, config_pk: int) -> dict[str, list[dict[str, Any]]]:
        """Fact rows of one configuration grouped by fact_key (rows in json_path order)."""
        if config_pk not in self._facts:
            grouped: dict[str, list[dict[str, Any]]] = {}
            for row in self.index.facts(config_pk=config_pk):
                grouped.setdefault(row["fact_key"], []).append(row)
            self._facts[config_pk] = grouped
        return self._facts[config_pk]


# ---------------------------------------------------------------------------------------------
# Filters (discovery narrowing, §6.5)


def _resolve_facet_value(s: _Session, facet: str, value: str, pointer: str,
                         warnings: list[dict[str, Any]]) -> str:
    """Canonical facet value for a filter (aliases resolved).

    A canonical vocabulary term with no records is not an error: it yields a FACET_VALUE_EMPTY
    warning (amendment 10.10). A value neither in the vocabulary nor in the index raises
    UNKNOWN_FACET_VALUE with up to 5 close values.
    """
    try:
        term, _ = s.vocab.resolve_term(facet, value)
    except LensError as error:
        error.details["pointer"] = pointer
        raise
    present = s.facet_values(facet)
    if term in present:
        return term
    if value in present:  # a record value outside the vocabulary is indexed verbatim (module_role)
        return value
    if s.vocab.is_known_term(facet, term):
        warnings.append({"code": "FACET_VALUE_EMPTY", "pointer": pointer, "facet": facet, "value": term,
                         "message": f"no indexed record has {facet} {term!r}"})
        return term
    close = s.vocab.close_terms(facet, value, sorted(present))
    raise LensError(
        "UNKNOWN_FACET_VALUE", f"{_short_text(value)!r} is not a value of facet {facet!r}",
        hint=("did you mean " + ", ".join(close) + "?") if close else
        f"list values with `os-optics-lens facets --facet {facet}`",
        details={"pointer": pointer, "facet": facet, "value": _short_text(value), "did_you_mean": close,
                 "values_count": len(present)})


def _unknown_facet(s: _Session, name: Any, pointer: str) -> LensError:
    close = difflib.get_close_matches(str(name), s.vocab.facet_names(), n=3, cutoff=0.5)
    return LensError("UNKNOWN_FACET", f"unknown facet {name!r}",
                     hint=("did you mean " + ", ".join(close) + "?") if close else
                     "facets: " + ", ".join(s.vocab.facet_names()),
                     details={"pointer": pointer, "facet": name, "did_you_mean": close,
                              "facets": s.vocab.facet_names()})


def _normalize_filters(s: _Session, raw: Any, pointer: str, warnings: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate and normalize a `filters` object (closed schema, aliases resolved, sorted)."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise _invalid("filters must be an object", pointer)
    _check_keys(raw, _FILTER_KEYS, pointer, "filters")
    out: dict[str, Any] = {}
    if "facets" in raw:
        facet_filters = raw["facets"]
        fptr = _ptr(pointer, "facets")
        if not isinstance(facet_filters, dict):
            raise _invalid("filters.facets must be an object of facet: [values]", fptr)
        normalized: dict[str, list[str]] = {}
        for name in sorted(facet_filters, key=str):
            if not s.vocab.has_facet(name):
                raise _unknown_facet(s, name, _ptr(fptr, name))
            values = _string_list(facet_filters[name], _ptr(fptr, name), f"facet {name}")
            normalized[name] = sorted({_resolve_facet_value(s, name, v, _ptr(fptr, name, i), warnings)
                                       for i, v in enumerate(values)})
        if normalized:
            out["facets"] = normalized
    if "collections" in raw:
        cptr = _ptr(pointer, "collections")
        values = _string_list(raw["collections"], cptr, "collections")
        out["collections"] = sorted({_resolve_facet_value(s, "collection", v, _ptr(cptr, i), warnings)
                                     for i, v in enumerate(values)})
    if "record_ids" in raw:
        rptr = _ptr(pointer, "record_ids")
        values = _string_list(raw["record_ids"], rptr, "record_ids")
        for i, record_id in enumerate(values):
            if s.record_by_id(record_id) is None:
                close = difflib.get_close_matches(record_id, s.index.record_ids(), n=5, cutoff=0.5)
                raise LensError("RECORD_NOT_FOUND", f"no record {record_id!r} in index snapshot {s.snapshot['id']}",
                                hint="Check the id, or find ids with `os-optics-lens search` or `facets`.",
                                details={"pointer": _ptr(rptr, i), "record_id": record_id, "did_you_mean": close})
        out["record_ids"] = sorted(set(values))
    if "ranges" in raw:
        out["ranges"] = _normalize_ranges(s, raw["ranges"], _ptr(pointer, "ranges"))
    if "spectral_span" in raw:
        span = raw["spectral_span"]
        sptr = _ptr(pointer, "spectral_span")
        if not (isinstance(span, list) and len(span) == 2 and all(_is_number(v) for v in span) and span[0] <= span[1]):
            raise _invalid("spectral_span must be [low_nm, high_nm] with low <= high", sptr, actual=span)
        out["spectral_span"] = [span[0], span[1]]
    return out


def _normalize_ranges(s: _Session, raw: Any, pointer: str) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise _invalid("ranges must be a list of {fact, min?, max?}", pointer)
    out = []
    for i, item in enumerate(raw):
        iptr = _ptr(pointer, i)
        if not isinstance(item, dict):
            raise _invalid("a range must be an object {fact, min?, max?}", iptr)
        _check_keys(item, _RANGE_KEYS, iptr, "range")
        fact = s.require_fact(item.get("fact"), _ptr(iptr, "fact"))
        rows = s.key_rows(fact)
        if not any(r["value_num"] is not None or r["value_min"] is not None or r["value_max"] is not None
                   for r in rows):
            raise _invalid(f"{fact} has no numeric values to range over", _ptr(iptr, "fact"),
                           "use a constraint (eq/in/includes_all) for non-numeric facts")
        low, high = item.get("min"), item.get("max")
        for name, value in (("min", low), ("max", high)):
            if value is not None and not _is_number(value):
                raise _invalid(f"range {name} must be a finite number", _ptr(iptr, name))
        if low is None and high is None:
            raise _invalid("a range needs min, max or both", iptr)
        if low is not None and high is not None and low > high:
            raise _invalid("range min must be <= max", iptr)
        out.append({"fact": fact, "min": low, "max": high})
    return out


def _row_in_range(row: dict[str, Any], low: float | None, high: float | None) -> bool:
    """A numeric fact row inside [low, high], or an interval row overlapping it."""
    if row["value_num"] is not None:
        value = row["value_num"]
        return (low is None or value >= low) and (high is None or value <= high)
    lo, hi = row["value_min"], row["value_max"]
    if lo is None or hi is None:
        return False
    return (high is None or lo <= high) and (low is None or hi >= low)


def _row_has_number(row: dict[str, Any]) -> bool:
    return row["value_num"] is not None or (row["value_min"] is not None and row["value_max"] is not None)


@dataclass
class _Scope:
    """Configurations surviving discovery filters, plus per-record exclusion counts."""

    configs: list[dict[str, Any]]
    excluded: dict[str, int]
    multi_valued: dict[int, list[dict[str, Any]]] = field(default_factory=dict)

    @property
    def record_pks(self) -> list[int]:
        seen: dict[int, None] = {}
        for config in self.configs:
            seen.setdefault(config["record_pk"], None)
        return list(seen)


def _filter_scope(s: _Session, filters: dict[str, Any]) -> _Scope:
    """Apply facets (AND across, OR within), collections, record ids, spectral span and ranges.

    Facets apply at their level: a record-level value admits every configuration of the record; a
    configuration-level value only that configuration. A record is excluded when none of its
    configurations survives; it is counted under the furthest stage any configuration reached
    (`filters` < `missing_fact` < `out_of_range`).
    """
    facet_filters = dict(filters.get("facets", {}))
    if "collections" in filters:
        wanted = set(filters["collections"])
        existing = set(facet_filters.get("collection", wanted))
        facet_filters["collection"] = sorted(existing & wanted)
    record_ids = set(filters.get("record_ids", ())) or None
    ranges = list(filters.get("ranges", ()))
    span = filters.get("spectral_span")
    range_rows: dict[str, dict[int, list[dict[str, Any]]]] = {}
    for item in ranges + ([{"fact": "spectrum.sampling_span_nm"}] if span is not None else []):
        if item["fact"] not in range_rows:
            grouped: dict[int, list[dict[str, Any]]] = {}
            for row in s.key_rows(item["fact"]):
                grouped.setdefault(row["config_pk"], []).append(row)
            range_rows[item["fact"]] = grouped
    survivors: list[dict[str, Any]] = []
    stage: dict[int, int] = {}
    multi: dict[int, list[dict[str, Any]]] = {}
    records = s.records()
    for config in s.configs():
        rpk = config["record_pk"]
        stage.setdefault(rpk, -1)
        if record_ids is not None and records[rpk]["record_id"] not in record_ids:
            stage[rpk] = max(stage[rpk], 0)
            continue
        if any(not (s.facets_of(config, name) & set(values)) for name, values in facet_filters.items()):
            stage[rpk] = max(stage[rpk], 0)
            continue
        failed = None
        notes: list[dict[str, Any]] = []
        checks = [(item["fact"], item["min"], item["max"]) for item in ranges]
        if span is not None:
            checks.append(("spectrum.sampling_span_nm", span[0], span[1]))
        for fact, low, high in checks:
            rows = [r for r in range_rows[fact].get(config["config_pk"], []) if _row_has_number(r)]
            if not rows:
                failed = 1
                break
            if not any(_row_in_range(r, low, high) for r in rows):
                failed = 2
                break
            if len(rows) > 1:
                notes.append({"range": fact, "multi_valued": True, "rows": len(rows)})
        if failed is not None:
            stage[rpk] = max(stage[rpk], failed)
            continue
        if notes:
            multi[config["config_pk"]] = notes
        survivors.append(config)
    surviving_records = {c["record_pk"] for c in survivors}
    excluded = {name: 0 for name in _EXCLUDED_STAGES}
    for rpk, reached in stage.items():
        if rpk not in surviving_records:
            excluded[_EXCLUDED_STAGES[max(reached, 0)]] += 1
    return _Scope(survivors, excluded, multi)


# ---------------------------------------------------------------------------------------------
# Request normalization (amendments 10.9, 10.10, 10.15)


@dataclass
class _Options:
    """Presentation options of one query (not part of the query digest)."""

    fields: list[str] | None
    detail: str
    limit: int
    cursor: str | None


def _normalize_query(s: _Session, raw: Any, base: str, warnings: list[dict[str, Any]], *,
                     allowed: frozenset[str] = _QUERY_KEYS) -> tuple[dict[str, Any], _Options]:
    """Validate one search query and return (normalized query, options).

    The normalized query holds exactly what the query digest covers: mode, filters, constraints,
    targets, preferences, include, hard_shortfall and evaluation_scope, after alias resolution,
    defaults and id defaulting (`c<i>`, `t<i>`, `p<i>`).
    """
    if not isinstance(raw, dict):
        raise _invalid("a query must be a JSON object", base or "/")
    _check_keys(raw, allowed, base, "query")
    mode = raw.get("mode")
    if mode is None:
        raise _invalid("mode is required", _ptr(base, "mode"), "use specification or starting_point",
                       allowed=list(MODES))
    if mode not in MODES:
        close = difflib.get_close_matches(str(mode), MODES, n=2, cutoff=0.5)
        raise _invalid(f"unknown mode {_short_text(mode)!r}", _ptr(base, "mode"), "use specification or starting_point",
                       did_you_mean=close, allowed=list(MODES))
    normalized: dict[str, Any] = {"mode": mode}
    normalized["filters"] = _normalize_filters(s, raw.get("filters"), _ptr(base, "filters"), warnings)

    constraints_raw = raw.get("constraints", [])
    cptr = _ptr(base, "constraints")
    if not isinstance(constraints_raw, list):
        raise _invalid("constraints must be a list", cptr)
    if mode == SPECIFICATION and not constraints_raw:
        raise _invalid("a specification search needs at least one constraint", cptr,
                       "use mode starting_point to rank without hard constraints, or `facets` to explore")
    constraints = []
    with _request_errors():
        for i, item in enumerate(constraints_raw):
            constraints.append(expand_constraint(item, None, i, pointer=_ptr(cptr, i)))
    for i, constraint in enumerate(constraints):
        if is_shorthand(constraint):
            _reject_interval(s, constraint, _ptr(cptr, i), "predicate", allow_cover=True)
    _check_unique_ids(constraints, cptr, "constraint")
    normalized["constraints"] = constraints

    targets_raw = raw.get("targets", [])
    tptr = _ptr(base, "targets")
    if not isinstance(targets_raw, list):
        raise _invalid("targets must be a list", tptr)
    if targets_raw and mode != STARTING_POINT:
        raise _invalid("targets apply only to mode starting_point", tptr,
                       "use a constraint for a hard requirement, or mode starting_point for flexible targets")
    targets = []
    for i, item in enumerate(targets_raw):
        with _request_errors():
            validate_target(item, _ptr(tptr, i))
        s.reject_check(item["fact"], _ptr(tptr, i, "fact"))
        s.reject_check(item.get("path"), _ptr(tptr, i, "path"))
        _reject_interval(s, item, _ptr(tptr, i), "target", allow_cover=False)
        target = copy.deepcopy(item)
        target.setdefault("id", f"t{i}")
        target.setdefault("op", "near")
        target.setdefault("weight", 1)
        targets.append(target)
    _check_unique_ids(targets, tptr, "target")
    normalized["targets"] = targets

    pptr = _ptr(base, "preferences")
    preferences_raw = raw.get("preferences", [])
    with _request_errors():
        validate_preferences(preferences_raw, pptr)
    preferences = []
    for i, item in enumerate(preferences_raw):
        key = ".".join(list(item["scope"]) + [item["metric"]]) if is_fixture_preference(item) else item["fact"]
        s.reject_check(key, _ptr(pptr, i, "metric" if is_fixture_preference(item) else "fact"))
        if not is_fixture_preference(item):
            s.reject_check(item.get("path"), _ptr(pptr, i, "path"))
            _reject_interval(s, item, _ptr(pptr, i), "preference", allow_cover=False)
        preference = copy.deepcopy(item)
        preference.setdefault("id", f"p{i}")
        preferences.append(preference)
    _check_unique_ids(preferences, pptr, "preference")
    normalized["preferences"] = preferences

    iptr = _ptr(base, "include")
    if "include" in raw:
        include = raw["include"]
        if not isinstance(include, list) or not include or not all(v in OUTCOMES for v in include) \
                or len(set(include)) != len(include):
            raise _invalid("include must be a non-empty list of distinct outcomes", iptr, allowed=list(OUTCOMES))
        normalized["include"] = [o for o in OUTCOMES if o in include]
    else:
        normalized["include"] = [MATCH, UNCONFIRMED] if mode == SPECIFICATION else list(OUTCOMES)

    hard = raw.get("hard_shortfall")
    hptr = _ptr(base, "hard_shortfall")
    if hard is not None:
        if mode != STARTING_POINT:
            raise _invalid("hard_shortfall applies only to mode starting_point", hptr)
        if hard not in HARD_SHORTFALL:
            raise _invalid("hard_shortfall must be exclude or allow", hptr, allowed=list(HARD_SHORTFALL))
    normalized["hard_shortfall"] = hard or "exclude"

    scope = raw.get("evaluation_scope", EVALUATION_SCOPES[0])
    if scope not in EVALUATION_SCOPES:
        raise _invalid("evaluation_scope accepts only one_nominal_configuration", _ptr(base, "evaluation_scope"),
                       allowed=list(EVALUATION_SCOPES))
    normalized["evaluation_scope"] = scope

    field_list = None
    if "fields" in raw:
        fptr = _ptr(base, "fields")
        field_list = _string_list(raw["fields"], fptr, "fields", max_items=MAX_SUMMARY_FIELDS)
        for i, key in enumerate(field_list):
            s.require_fact(key, _ptr(fptr, i))
        field_list = list(dict.fromkeys(field_list))
    detail = raw.get("detail", "compact")
    if detail not in DETAILS:
        raise _invalid("detail must be compact or full", _ptr(base, "detail"), allowed=list(DETAILS))
    limit = _check_int(raw.get("limit"), _ptr(base, "limit"), "limit", 1, MAX_LIMIT, DEFAULT_LIMIT)
    cursor = raw.get("cursor")
    if cursor is not None and not isinstance(cursor, str):
        raise LensError("CURSOR_INVALID", "cursor must be the next_cursor string or null",
                        details={"pointer": _ptr(base, "cursor")})
    return normalized, _Options(field_list, detail, limit, cursor)


def _interval_paths(s: _Session, fact: str) -> list[str] | None:
    """The min_/max_ member pair when `fact` (an exact indexed key) is an interval without a value
    member in some configuration, else None."""
    if fact not in s.fact_keys():
        return None
    for row in s.key_rows(fact):
        if row["value_num"] is None and row["value_paths"]:
            paths = json.loads(row["value_paths"])
            lows = [p for p in paths if p.startswith("min_")]
            if lows and ("max_" + lows[0][4:]) in paths:
                return [lows[0], "max_" + lows[0][4:]]
    return None


def _reject_interval(s: _Session, item: dict[str, Any], pointer: str, what: str, *, allow_cover: bool) -> None:
    """Amendments 10.3/10.9: an interval fact needs an explicit bound path (except covers_interval
    on the interval itself), and can never be a target or preference value."""
    if "path" in item or "scope" in item:
        return
    if allow_cover and item.get("op") == "covers_interval":
        return
    if not allow_cover and "required_sampled_field_fractions" in item and item["fact"].startswith("performance."):
        # The selected grid may resolve to a numeric entry even when another entry is an interval.
        return
    pair = _interval_paths(s, item["fact"])
    if pair is not None:
        raise _invalid(f"{item['fact']} is an interval ({pair[0]}/{pair[1]}); a {what} needs one bound",
                       _ptr(pointer, "fact"), f"give path {pair[0]} or {pair[1]}", paths=pair)


def _check_unique_ids(items: Sequence[dict[str, Any]], pointer: str, what: str) -> None:
    seen: dict[str, int] = {}
    for i, item in enumerate(items):
        ident = item["id"]
        if ident in seen:
            raise _invalid(f"duplicate {what} id {ident!r}", _ptr(pointer, i, "id"), first=seen[ident])
        seen[ident] = i


# ---------------------------------------------------------------------------------------------
# Fact summaries (§6.6, amendment 10.7)


def _row_value(row: dict[str, Any]) -> Any:
    """The JSON value a fact row stands for (intervals as [min, max]; studies as their state)."""
    if row["value_json"] is None:
        return None
    value = json.loads(row["value_json"])
    value = public_fact_value(value)
    if isinstance(value, dict):
        return {"state": row["state"]} if row["state"] is not None else {"members": sorted(value)[:8]}
    return value


def _summary_entry(rows: list[dict[str, Any]], full_basis: bool) -> dict[str, Any]:
    """`{value, unit, basis}` for one row; `{values: n, min, max, unit}` when multi-valued."""
    if len(rows) == 1:
        row = rows[0]
        entry: dict[str, Any] = {"value": _row_value(row), "unit": row["unit"],
                                 "basis": row["basis"] if full_basis else row["basis_class"]}
        if row["value_json"] is None and row["value_paths"]:
            entry["value_paths"] = json.loads(row["value_paths"])
        if row["fact_key"] in _PARAXIAL_LENGTHS and row["basis_class"] == "ENGINE_COMPUTED" and row["conditions_json"]:
            wavelength = json.loads(row["conditions_json"]).get("wavelength_nm")
            if _is_number(wavelength):
                entry["conditions"] = {"wavelength_nm": wavelength}
        if full_basis:
            for name in ("eligible_for", "state"):
                if row[name] is not None:
                    entry[name] = row[name]
        elif row["eligible_for"] is not None:
            entry["eligible_for"] = row["eligible_for"]
        return entry
    numbers = [r["value_num"] for r in rows if r["value_num"] is not None]
    numbers += [v for r in rows for v in (r["value_min"], r["value_max"]) if v is not None]
    units = sorted({r["unit"] for r in rows if r["unit"] is not None})
    return {"values": len(rows), "min": min(numbers) if numbers else None,
            "max": max(numbers) if numbers else None, "unit": units[0] if len(units) == 1 else (units or None)}


def _system_aperture_keys(facts: dict[str, list[dict[str, Any]]]) -> list[str]:
    return sorted(key for key, rows in facts.items() if any(r["system_aperture"] for r in rows))


def _default_summary_keys(facts: dict[str, list[dict[str, Any]]]) -> list[str]:
    """Default summary facts (§6.6): EFL, the system aperture, the field, image circle, total track
    and sampling span, each when present in the configuration."""
    keys = [k for k in _DEFAULT_LEADING if k in facts]
    keys += _system_aperture_keys(facts)
    field_key = next((k for k in _FIELD_CANDIDATES if k in facts), None)
    if field_key:
        keys.append(field_key)
    keys += [k for k in _DEFAULT_TRAILING if k in facts]
    return keys


def _named_fact_keys(s: _Session, q: dict[str, Any]) -> list[str]:
    """Fact keys named by targets, preferences and constraints (longest indexed prefix)."""
    keys: list[str] = []
    for target in q["targets"]:
        keys.append(s.known_prefix(target["fact"]) or target["fact"])
    for preference in q["preferences"]:
        name = ".".join(list(preference["scope"]) + [preference["metric"]]) \
            if is_fixture_preference(preference) else preference["fact"]
        keys.append(s.known_prefix(name) or name)
    for constraint in q["constraints"]:
        if is_shorthand(constraint):
            name = constraint["fact"]
        else:
            name = constraint_fact_key(constraint)
            path = (constraint.get("predicates") or [{}])[0].get("path") or ""
            if path:
                name = f"{name}.{path}"
        prefix = s.known_prefix(name)
        if prefix:
            keys.append(prefix)
    return list(dict.fromkeys(keys))


def _summary_facts(s: _Session, config_pk: int, keys: Sequence[str] | None, named: Sequence[str],
                   full_basis: bool) -> dict[str, Any]:
    facts = s.config_facts(config_pk)
    if keys is None:
        ordered = list(dict.fromkeys(list(named) + _default_summary_keys(facts)))
        ordered = [k for k in ordered if k in facts][:MAX_SUMMARY_FIELDS]
    else:
        ordered = list(keys)
    out: dict[str, Any] = {}
    for key in ordered:
        rows = facts.get(key)
        out[key] = _summary_entry(rows, full_basis) if rows else None
    return out


# ---------------------------------------------------------------------------------------------
# Search evaluation (§6.4, §6.5)


@dataclass
class _ConfigResult:
    """One evaluated configuration."""

    config: dict[str, Any]
    evaluation: dict[str, Any]
    targets: list[dict[str, Any]]
    preferences: list[dict[str, Any]]
    rank_key: list[Any] = field(init=False)
    sort_key: tuple[Any, ...] = field(init=False)
    choice_key: tuple[Any, ...] = field(init=False)
    """Chooses the record's best configuration (§6.4); `sort_key` then orders the records."""


def _pref_sort(entry: list[Any]) -> tuple[int, float]:
    return (1, 0.0) if entry[0] else (0, float(entry[1]))


def _ranking_comparison(s: _Session, config: dict[str, Any], info: dict[str, Any]) -> dict[str, Any]:
    """Comparison metadata for the resolved value, including the indexed performance qualifiers."""
    comparison = {key: info.get(key) for key in ("sampled_field_fractions", "conditions", "unit", "value_member")}
    comparison["qualifiers"] = None
    entry_path = info["json_path"].split(".", 1)[0]
    if entry_path.startswith("performance["):
        fact = ".".join(info["fact"].split(".")[:2])
        rows = s.config_facts(config["config_pk"]).get(fact, ())
        comparison["qualifiers"] = next(
            (row["qualifiers_json"] for row in rows if row["json_path"].split(".", 1)[0] == entry_path), None)
    return comparison


def _evaluate_targets(s: _Session, config: dict[str, Any], view: dict[str, Any], q: dict[str, Any],
                      base: str) -> list[dict[str, Any]]:
    out = []
    for i, target in enumerate(q["targets"]):
        with _request_errors():
            value, reason, info = target_value(view, target, s.ctx, pointer=_ptr(base, "targets", i))
        item: dict[str, Any] = {"id": target["id"], "fact": target["fact"], "weight": target["weight"],
                                "value": value}
        if value is None:
            item["reason"] = reason
            if info.get("details"):
                item["details"] = info["details"]
        else:
            item["unit"] = info.get("unit")
            item["comparison"] = _ranking_comparison(s, config, info)
        out.append(item)
    return out


def _evaluate_preferences(s: _Session, config: dict[str, Any], view: dict[str, Any], q: dict[str, Any],
                          base: str) -> list[dict[str, Any]]:
    out = []
    for i, preference in enumerate(q["preferences"]):
        with _request_errors():
            value, reason, info = preference_value(view, preference, s.ctx, pointer=_ptr(base, "preferences", i))
        fact = info.get("fact") or preference.get("fact")
        item: dict[str, Any] = {"id": preference["id"], "fact": fact, "direction": preference["direction"],
                                "value": value}
        if value is None:
            item["reason"] = reason
            if info.get("details"):
                item["details"] = info["details"]
        else:
            item["comparison"] = _ranking_comparison(s, config, info)
        out.append(item)
    return out


def _evaluate_config(s: _Session, config: dict[str, Any], q: dict[str, Any], base: str) -> _ConfigResult:
    view = s.view(config)
    with _request_errors():
        evaluation = evaluate_configuration(view, q["constraints"], s.ctx, detail=True)
    targets = _evaluate_targets(s, config, view, q, base)
    preferences = _evaluate_preferences(s, config, view, q, base)
    return _ConfigResult(config, evaluation, targets, preferences)


def _hard_rank(result: _ConfigResult, mode: str) -> tuple[int, ...]:
    if mode == SPECIFICATION:
        # Choose the record's best configuration by outcome and hard-constraint counts before preferences.
        return configuration_rank(result.evaluation, result.config["ordinal"])[:-1]
    counts = result.evaluation["counts"]
    return counts[DOES_NOT_MATCH], counts[UNCONFIRMED]


def _sampled_ranking_unknowns(results: Sequence[_ConfigResult]) -> None:
    """Keep each sampled ranking term only when its available values can be compared."""
    for terms in zip(*(result.targets + result.preferences for result in results)):
        known = [term for term in terms if "comparison" in term]
        if not any(term["comparison"]["sampled_field_fractions"] is not None for term in known):
            continue
        first = known[0]["comparison"]
        differs = []
        for key, value in first.items():
            equal = ranking_conditions_equal if key == "conditions" else json_equal
            if any(not equal(value, term["comparison"][key]) for term in known[1:]):
                differs.append(key)
        if not differs:
            continue
        grids_differ = "sampled_field_fractions" in differs
        hint = ("pin required_sampled_field_fractions in the target or shorthand preference" if grids_differ
                else "narrow candidates to matching conditions, qualifiers and units")
        for term in known:
            comparison = term["comparison"]
            details = {"actual": comparison["sampled_field_fractions"], "differs_by": differs, "hint": hint}
            for key in differs:
                if key == "sampled_field_fractions":
                    continue
                value = comparison[key]
                if key == "qualifiers" and value is not None:
                    value = json.loads(value)
                details[key] = public_fact_value(value)
            term.update(value=None, reason=SAMPLING_UNSPECIFIED if grids_differ else CONDITIONS_DIFFER, details=details)


def _rank_config(result: _ConfigResult, record_id: str, q: dict[str, Any], base: str) -> None:
    ordinal = result.config["ordinal"]
    entries = [preference_rank_entry(p["value"], p["direction"]) for p in result.preferences]
    sorted_preferences = tuple(_pref_sort(entry) for entry in entries)
    hard = _hard_rank(result, q["mode"])
    if q["mode"] == SPECIFICATION:
        result.rank_key = [result.evaluation["outcome"], *entries, record_id, ordinal]
        result.sort_key = (hard[0], *sorted_preferences, record_id, ordinal)
        result.choice_key = (*hard, *sorted_preferences, ordinal)
        return
    for i, (item, target) in enumerate(zip(result.targets, q["targets"])):
        if item["value"] is not None:
            with _request_errors():
                item.update(target_deviation(item["value"], target, pointer=_ptr(base, "targets", i)))
    unknown_targets = sum(1 for item in result.targets if "reason" in item)
    score = round(sum(t["weight"] * t["normalized"] for t in result.targets if "normalized" in t), 9)
    if not math.isfinite(score):
        raise _invalid("the weighted target score is not a finite number", _ptr(base, "targets"),
                       "give smaller weights or larger tolerances")
    result.rank_key = [*hard, unknown_targets, score, *entries, record_id, ordinal]
    result.sort_key = (*hard, unknown_targets, score, *sorted_preferences, record_id, ordinal)
    result.choice_key = result.sort_key


def _explanation(q: dict[str, Any], best: _ConfigResult) -> str:
    """One English sentence explaining a candidate's rank key."""
    counts = best.evaluation["counts"]
    total = sum(counts.values())
    pref_parts = []
    for p in best.preferences:
        if p["value"] is None:
            message = f"preference {p['fact']} unknown ({p['reason']})"
            if "differs_by" not in p.get("details", {}):
                message += ", ranked after comparable values"
            pref_parts.append(message)
        else:
            pref_parts.append(f"preference {p['fact']} = {p['value']} ({p['direction']})")
    if q["mode"] == SPECIFICATION:
        text = (f"{best.evaluation['outcome']}: {counts[MATCH]} of {total} constraints match, "
                f"{counts[UNCONFIRMED]} unconfirmed, {counts[DOES_NOT_MATCH]} do not match")
    else:
        text = (f"{counts[DOES_NOT_MATCH]} hard shortfall(s), {counts[UNCONFIRMED]} unconfirmed hard "
                f"constraint(s), {sum(1 for t in best.targets if 'reason' in t)} unknown target(s)")
        known = [t for t in best.targets if "normalized" in t]
        if known:
            score = round(sum(t["weight"] * t["normalized"] for t in known), 9)
            details = ", ".join(
                f"{t['fact']} {t['value']:g} vs {t['target']:g} ("
                f"{'' if t['weight'] == 1 else format(t['weight'], 'g') + ' x '}"
                f"{t['normalized']:.3g} x "
                f"{'absolute deviation' if t['scale_basis'] == 'absolute' else t['scale_basis']})"
                for t in known)
            text += f"; target score {score:g} from {details}"
    if pref_parts:
        text += "; " + "; ".join(pref_parts)
    return text + "."


def _compact_constraint(evaluation: dict[str, Any]) -> Any:
    """Compact constraint result (amendment 10.11): bare "MATCH" only when fully pinned."""
    outcome = evaluation["outcome"]
    if outcome == MATCH:
        if evaluation.get("unpinned"):
            return {"outcome": MATCH, "unpinned": evaluation["unpinned"]}
        return MATCH
    if outcome == UNCONFIRMED:
        return {"outcome": UNCONFIRMED, "reason": evaluation["reason"]}
    return {"outcome": DOES_NOT_MATCH}


def _shortfalls(q: dict[str, Any], best: _ConfigResult) -> list[dict[str, Any]]:
    """Hard DOES_NOT_MATCH gaps, then target deviations."""
    out: list[dict[str, Any]] = []
    constraints = {c["id"]: c for c in q["constraints"]}
    for evaluation in best.evaluation["constraints"]:
        if evaluation["outcome"] != DOES_NOT_MATCH:
            continue
        if evaluation.get("shortfalls"):
            out.extend(evaluation["shortfalls"])
            continue
        item: dict[str, Any] = {"constraint": evaluation["id"]}
        constraint = constraints.get(evaluation["id"], {})
        predicates = evaluation.get("predicates") or []
        expected = ([constraint.get("value")] if is_shorthand(constraint)
                    else [p.get("value") for p in constraint.get("predicates", [])])
        failing = []
        for position, predicate in enumerate(predicates):
            if predicate.get("outcome") == DOES_NOT_MATCH:
                failing.append({"path": predicate.get("path"), "op": predicate.get("op"),
                                "value": predicate.get("value"),
                                "expected": expected[position] if position < len(expected) else None})
        if failing:
            item["predicates"] = failing
        if evaluation.get("details"):
            item["details"] = evaluation["details"]
        out.append(item)
    for target in best.targets:
        if "normalized" in target:
            out.append({"target": target["id"], "fact": target["fact"], "value": target["value"],
                        "target_value": target["target"], "deviation": target["deviation"],
                        "normalized": target["normalized"], "scale_basis": target["scale_basis"],
                        "weight": target["weight"], "unit": target.get("unit")})
    return out


def _ranking_unknown_details(item: dict[str, Any], with_details: bool) -> dict[str, Any] | None:
    details = item.get("details")
    if with_details or not details:
        return details
    if "differs_by" in details:
        return {key: details[key] for key in ("actual", "differs_by", "hint")}
    return None


def _unknowns(best: _ConfigResult, with_details: bool) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for evaluation in best.evaluation["constraints"]:
        if evaluation["outcome"] == UNCONFIRMED:
            item = {"constraint": evaluation["id"], "reason": evaluation["reason"]}
            if with_details and evaluation.get("details"):
                item["details"] = evaluation["details"]
            out.append(item)
    for target in best.targets:
        if "reason" in target:
            item = {"target": target["id"], "fact": target["fact"], "reason": target["reason"]}
            details = _ranking_unknown_details(target, with_details)
            if details:
                item["details"] = details
            out.append(item)
    for preference in best.preferences:
        if preference["value"] is None:
            item = {"preference": preference["id"], "fact": preference["fact"], "reason": preference["reason"]}
            details = _ranking_unknown_details(preference, with_details)
            if details:
                item["details"] = details
            out.append(item)
    return out


def _candidate_forms(s: _Session, q: dict[str, Any], options: _Options, named: list[str], record: dict[str, Any],
                     best: _ConfigResult, others: list[_ConfigResult], position: int, scope: _Scope
                     ) -> tuple[dict[str, Any], dict[str, Any]]:
    """The full and the compact (budget fallback) candidate objects."""
    config = best.config
    full_detail = options.detail == "full"
    head: dict[str, Any] = {
        "record_id": record["record_id"], "revision": record["revision"], "title": record["title"],
        "collection": record["collection_id"], "family": record["family_id"],
        "configuration_id": config["configuration_id"], "outcome": best.evaluation["outcome"],
        "counts": dict(best.evaluation["counts"]),
    }
    compact_constraints = {e["id"]: _compact_constraint(e) for e in best.evaluation["constraints"]}
    if full_detail:
        constraints = {e["id"]: {k: v for k, v in e.items() if k != "id"} for e in best.evaluation["constraints"]}
    else:
        constraints = compact_constraints
    rank = {"position": position, "key": best.rank_key, "explanation": _explanation(q, best)}
    shortfalls = _shortfalls(q, best)
    tail: dict[str, Any] = {}
    if others:
        tail["other_configurations"] = [
            {"configuration_id": o.config["configuration_id"], "outcome": o.evaluation["outcome"],
             "counts": dict(o.evaluation["counts"])} for o in others]
    notes = scope.multi_valued.get(config["config_pk"])
    if notes:
        tail["notes"] = notes
    full = {**head, "constraints": constraints, "shortfalls": shortfalls,
            "unknowns": _unknowns(best, full_detail),
            "facts": _summary_facts(s, config["config_pk"], options.fields, named, full_detail),
            "rank": rank, **tail}
    compact = {**head, "constraints": compact_constraints, "shortfalls": shortfalls,
               "unknowns": _unknowns(best, False), "rank": rank, **tail, "compacted": True}
    return full, compact


@dataclass
class _Prepared:
    """A fully evaluated query ready for page filling."""

    normalized: dict[str, Any]
    digest: str
    offset: int
    listed: int
    result: dict[str, Any]
    full: list[dict[str, Any]]
    compact: list[dict[str, Any]]


def _unknown_shorthand_warnings(s: _Session, q: dict[str, Any], evaluated_views: list[dict[str, Any]],
                                base: str) -> list[dict[str, Any]]:
    """UNKNOWN_FIELD warnings for shorthand facts absent from every configuration view (10.3)."""
    warnings = []
    for i, constraint in enumerate(q["constraints"]):
        if not is_shorthand(constraint):
            continue
        fact = constraint["fact"]
        if any(fact_present(view, fact) for view in evaluated_views):
            continue
        if any(fact_present(view, fact) for view in s.all_views()):
            continue
        close = s.did_you_mean(fact)
        warnings.append({"code": "UNKNOWN_FIELD", "pointer": _ptr(base, "constraints", i, "fact"),
                         "message": f"{fact} is absent from every configuration; the constraint is "
                                    "UNCONFIRMED (FACT_MISSING) everywhere",
                         "did_you_mean": close})
    return warnings


def _prepare_query(s: _Session, raw: Any, base: str, warnings: list[dict[str, Any]], *,
                   allowed: frozenset[str] = _QUERY_KEYS) -> _Prepared:
    q, options = _normalize_query(s, raw, base, warnings, allowed=allowed)
    digest = output.query_digest(q)
    offset = 0
    if options.cursor is not None:
        offset = output.decode_cursor(options.cursor, s.snapshot["id"], digest, _ptr(base, "cursor"))
    scope = _filter_scope(s, q["filters"])
    records = s.records()
    per_record: dict[int, list[_ConfigResult]] = {}
    for config in scope.configs:
        result = _evaluate_config(s, config, q, base)
        per_record.setdefault(config["record_pk"], []).append(result)
    warnings.extend(_unknown_shorthand_warnings(s, q, [s.view(c) for c in scope.configs], base))

    total = {"evaluated": len(per_record), MATCH: 0, UNCONFIRMED: 0, DOES_NOT_MATCH: 0,
             "excluded": {**scope.excluded, "hard_shortfall": 0, "outcome": 0}, "listed": 0}
    rankable: dict[int, list[_ConfigResult]] = {}
    for record_pk, results in per_record.items():
        best_hard = min(_hard_rank(result, q["mode"]) for result in results)
        eligible = [result for result in results if _hard_rank(result, q["mode"]) == best_hard]
        outcome = eligible[0].evaluation["outcome"]
        if q["mode"] == STARTING_POINT and q["hard_shortfall"] == "exclude" and outcome == DOES_NOT_MATCH:
            total["excluded"]["hard_shortfall"] += 1
            continue
        total[outcome] += 1
        if outcome not in q["include"]:
            total["excluded"]["outcome"] += 1
            continue
        rankable[record_pk] = eligible
    _sampled_ranking_unknowns([result for results in rankable.values() for result in results])

    ranked: list[tuple[tuple[Any, ...], dict[str, Any], _ConfigResult, list[_ConfigResult]]] = []
    for record_pk, eligible in rankable.items():
        record = records[record_pk]
        for result in eligible:
            _rank_config(result, record["record_id"], q, base)
        best = min(eligible, key=lambda r: r.choice_key)
        others = [r for r in per_record[record_pk] if r is not best]
        ranked.append((best.sort_key, record, best, others))
    ranked.sort(key=lambda item: item[0])
    total["listed"] = len(ranked)
    named = _named_fact_keys(s, q)
    full: list[dict[str, Any]] = []
    compact: list[dict[str, Any]] = []
    for position, (_, record, best, others) in enumerate(ranked[offset: offset + options.limit], start=offset + 1):
        full_form, compact_form = _candidate_forms(s, q, options, named, record, best, others, position, scope)
        full.append(full_form)
        compact.append(compact_form)
    result = {"mode": q["mode"], "total": total, "candidates": [],
              "page": {"limit": options.limit, "offset": offset, "returned": 0, "next_cursor": None}}
    return _Prepared(q, digest, offset, len(ranked), result, full, compact)


def _slot(s: _Session, prepared: _Prepared, entry_index: int | None) -> output.PageSlot:
    snapshot_id, digest = s.snapshot["id"], prepared.digest
    return output.PageSlot(
        page_holder=prepared.result, full=prepared.full, compact=prepared.compact,
        offset=prepared.offset, listed=prepared.listed,
        cursor_at=lambda offset: output.encode_cursor(snapshot_id, digest, offset),
        entry_index=entry_index)


def _envelope(s: _Session, body: dict[str, Any], warnings: list[dict[str, Any]]) -> dict[str, Any]:
    response: dict[str, Any] = {"ok": True, "snapshot": s.snapshot, **body}
    if warnings:
        response["warnings"] = warnings
    return response


def _budget(request_value: Any, argument: int | None, pointer: str) -> int:
    if argument is not None:
        return output.check_max_output_bytes(argument, "max_output_bytes")
    return output.check_max_output_bytes(request_value, pointer)


def search(index: Index, request: Any, *, max_output_bytes: int | None = None) -> dict[str, Any]:
    """Run one search query (no `queries` member); see `run_request`."""
    if isinstance(request, dict) and "queries" in request:
        raise _invalid("search takes a single query; use run_request for a batch", "/queries")
    return run_request(index, request, max_output_bytes=max_output_bytes)


def run_request(index: Index, request: Any, *, max_output_bytes: int | None = None) -> dict[str, Any]:
    """Run a single search query or a batch `{"queries": [...], "max_output_bytes"?}`.

    Single: `{"ok", "snapshot", "result": {mode, total, candidates, page}, "warnings"?, "bounds"}`.
    Batch: `{"ok", "snapshot", "results": [{index, ok, result | error}], "warnings"?, "bounds"}`
    where a failing sub-query becomes an error entry; `ok` is false only when every sub-query
    failed. The budget is `max_output_bytes` (argument, else the request member, else 32768) and
    covers the whole serialized response.
    """
    s = _Session(index)
    if not isinstance(request, dict):
        raise _invalid("the request must be a JSON object", "/")
    if "queries" in request:
        budget = _budget(request.get("max_output_bytes"), max_output_bytes, "/max_output_bytes")
        with _bounded_errors(budget):
            return _run_batch(s, request, budget)
    budget = _budget(request.get("max_output_bytes"), max_output_bytes, "/max_output_bytes")
    warnings: list[dict[str, Any]] = []
    with _bounded_errors(budget):
        prepared = _prepare_query(s, request, "", warnings)
        response = _envelope(s, {"result": prepared.result}, warnings)
        return output.fill_pages(response, [_slot(s, prepared, None)], budget)


@contextmanager
def _bounded_errors(budget: int) -> Iterator[None]:
    """Mark a failure with the request's byte budget: the CLI bounds the error envelope by it."""
    try:
        yield
    except LensError as error:
        error.output_budget = budget
        raise


def _run_batch(s: _Session, request: dict[str, Any], budget: int) -> dict[str, Any]:
    _check_keys(request, _BATCH_KEYS, "", "batch request")
    queries = request["queries"]
    if not isinstance(queries, list) or not queries or len(queries) > MAX_BATCH_QUERIES:
        raise _invalid(f"queries must be a list of 1 to {MAX_BATCH_QUERIES} queries", "/queries",
                       allowed={"min": 1, "max": MAX_BATCH_QUERIES})
    warnings: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    slots: list[output.PageSlot] = []
    for i, raw in enumerate(queries):
        base = _ptr("/queries", i)
        try:
            prepared = _prepare_query(s, raw, base, warnings, allowed=_SUB_QUERY_KEYS)
        except LensError as error:
            results.append(_error_entry(i, error))
            continue
        results.append({"index": i, "ok": True, "result": prepared.result})
        slots.append(_slot(s, prepared, i))
    response = _envelope(s, {"results": results}, warnings)
    response = output.fill_pages(response, slots, budget, batch=True, error_entry=_error_entry)
    response["ok"] = any(entry["ok"] for entry in response["results"])
    if not response["ok"]:
        response = output.finalize(response, budget)
    return response


_ERROR_ENTRY_TEXT_LIMIT = 256


def _error_entry(i: int, error: LensError) -> dict[str, Any]:
    """A failed batch entry; values echoed from the request are clipped so that one long input
    cannot crowd the other entries out of the budget (`details.truncated` marks a clip)."""
    return {"index": i, "ok": False, "error": output.clip_error(error.to_dict(), _ERROR_ENTRY_TEXT_LIMIT)}


def normalize_request(index: Index, request: Any) -> dict[str, Any]:
    """The normalized form of a single query or batch (what the query digests cover), with digests.

    Returns `{"queries": [{"normalized", "query_digest", "options"}]}` for a batch, else one such
    object; raises the same errors as `run_request` would for an invalid request.
    """
    s = _Session(index)
    if not isinstance(request, dict):
        raise _invalid("the request must be a JSON object", "/")

    def one(raw: Any, base: str, allowed: frozenset[str]) -> dict[str, Any]:
        q, options = _normalize_query(s, raw, base, [], allowed=allowed)
        digest = output.query_digest(q)
        echoed: dict[str, Any] = {"fields": options.fields, "detail": options.detail,
                                  "limit": options.limit, "cursor": options.cursor}
        if options.cursor is not None:
            # The same checks a search makes, so a continuation request that would fail is not
            # reported as fine (CURSOR_INVALID, CURSOR_MISMATCH, SNAPSHOT_MISMATCH).
            echoed["cursor_offset"] = output.decode_cursor(options.cursor, s.snapshot["id"], digest,
                                                           _ptr(base, "cursor"))
        return {"normalized": q, "query_digest": digest, "options": echoed}

    if "queries" in request:
        _check_keys(request, _BATCH_KEYS, "", "batch request")
        queries = request["queries"]
        if not isinstance(queries, list) or not queries or len(queries) > MAX_BATCH_QUERIES:
            raise _invalid(f"queries must be a list of 1 to {MAX_BATCH_QUERIES} queries", "/queries")
        return {"queries": [one(raw, _ptr("/queries", i), _SUB_QUERY_KEYS) for i, raw in enumerate(queries)]}
    return one(request, "", _QUERY_KEYS)


def explain_request(index: Index, request: Any, *, max_output_bytes: int | None = None) -> dict[str, Any]:
    """Response envelope echoing `normalize_request` (for `--explain-request`), bounded like the
    search: by `max_output_bytes` (the argument), else the request's own member, else 32768. An
    explanation that does not fit raises OUTPUT_LIMIT_EXCEEDED (details.required_bytes)."""
    s = _Session(index)
    if not isinstance(request, dict):
        raise _invalid("the request must be a JSON object", "/")
    budget = _budget(request.get("max_output_bytes"), max_output_bytes, "/max_output_bytes")
    with _bounded_errors(budget):
        result = normalize_request(index, request)
        return output.finalize(_envelope(s, {"result": result}, []), budget)


def fixture_to_request(case: dict[str, Any]) -> dict[str, Any]:
    """The search request for an acceptance fixture (amendment 9.1).

    `{mode: specification, filters: {record_ids: [case.record.id]}, constraints: case.constraints,
    preferences: [case.ranking_preference], evaluation_scope: case.evaluation_scope, detail: full}`;
    the preference is omitted when the fixture has none.
    """
    if not isinstance(case, dict) or not isinstance(case.get("record"), dict) or "constraints" not in case:
        raise _invalid("a fixture needs record.id and constraints", "/")
    request: dict[str, Any] = {
        "mode": SPECIFICATION,
        "filters": {"record_ids": [case["record"]["id"]]},
        "constraints": copy.deepcopy(case["constraints"]),
    }
    if case.get("ranking_preference") is not None:
        request["preferences"] = [copy.deepcopy(case["ranking_preference"])]
    if case.get("evaluation_scope") is not None:
        request["evaluation_scope"] = case["evaluation_scope"]
    request["detail"] = "full"
    return request


# ---------------------------------------------------------------------------------------------
# facets (§6.7)


def facets(index: Index, filters: Any = None, facets: Sequence[str] | None = None,
           limit_per_facet: int = DEFAULT_FACET_LIMIT, *, describe: bool = False,
           max_output_bytes: int | None = None) -> dict[str, Any]:
    """Facet values with record counts within the filtered scope.

    Result: `{"records", "configurations", "facets": {facet: {"level", "values": [[value, count],
    ...] by count desc then value, "more": n, "description"?}}}`. Counts are distinct records having
    the value in at least one configuration in scope.
    """
    s = _Session(index)
    budget = output.check_max_output_bytes(max_output_bytes, "max_output_bytes")
    limit = _check_int(limit_per_facet, "/limit", "limit", 1, MAX_FACET_LIMIT, DEFAULT_FACET_LIMIT)
    warnings: list[dict[str, Any]] = []
    normalized = _normalize_filters(s, filters, "/filters", warnings)
    names = s.vocab.facet_names()
    if facets is not None:
        requested = _string_list(list(facets), "/facets", "facets")
        for i, name in enumerate(requested):
            if not s.vocab.has_facet(name):
                raise _unknown_facet(s, name, _ptr("/facets", i))
        names = list(dict.fromkeys(requested))
    scope = _filter_scope(s, normalized)
    out: dict[str, Any] = {}
    for name in names:
        holders: dict[str, set[int]] = {}
        for config in scope.configs:
            for value in s.facets_of(config, name):
                holders.setdefault(value, set()).add(config["record_pk"])
        ranked = sorted(((value, len(pks)) for value, pks in holders.items()), key=lambda item: (-item[1], item[0]))
        entry: dict[str, Any] = {"level": s.vocab.facet_level(name),
                                 "values": [[value, count] for value, count in ranked[:limit]],
                                 "more": max(0, len(ranked) - limit)}
        if describe:
            entry["description"] = s.vocab.facet_description(name)
        out[name] = entry
    result = {"records": len(scope.record_pks), "configurations": len(scope.configs), "facets": out}
    return output.finalize(_envelope(s, {"result": result}, warnings), budget)


# ---------------------------------------------------------------------------------------------
# fields (§6.7, amendment 10.12)


def _row_type(row: dict[str, Any]) -> str:
    if row["value_num"] is not None:
        return "number"
    form = json.loads(row["constraint_form"]) if row["constraint_form"] else {}
    if row["value_min"] is not None or row["value_max"] is not None or (
            form.get("path") == "" and row["value_paths"] and row["metric"] is None) or (
            row["metric"] is not None and row["value_json"] is not None
            and isinstance(json.loads(row["value_json"]), list) and row["value_paths"]):
        return "interval"
    if row["value_json"] is None:
        return "none"
    value = json.loads(row["value_json"])
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    return "number" if _is_number(value) else "none"


def _one_or_list(values: Sequence[Any]) -> Any:
    distinct = sorted(set(values), key=lambda v: (v is None, str(v)))
    if not distinct:
        return None
    return distinct[0] if len(distinct) == 1 else distinct


def _public_condition_json(text: str | None) -> str | None:
    if text is None or '"source"' not in text:
        return text
    value = public_fact_value(json.loads(text))
    return canonical_key(value) if value else None


def _variant(row: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """What makes two values of a fact key comparable: conditions, qualifiers and sampling grid."""
    return (_public_condition_json(row["conditions_json"]), _public_condition_json(row["qualifiers_json"]),
            row["sampled_field_fractions_json"])


def _field_summary(s: _Session, key: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    numbers = [r["value_num"] for r in rows if r["value_num"] is not None]
    numbers += [v for r in rows for v in (r["value_min"], r["value_max"]) if v is not None]
    variants = {_variant(r) for r in rows}
    condition_keys = sorted({key for conditions, _, _ in variants if conditions for key in json.loads(conditions)})
    return {
        "records": len({r["record_pk"] for r in rows}),
        "unit": _one_or_list([r["unit"] for r in rows if r["unit"] is not None]),
        "value_type": _one_or_list([_row_type(r) for r in rows]),
        "bases": sorted({r["basis_class"] for r in rows if r["basis_class"] is not None}),
        "condition_variants": len(variants) if variants != {(None, None, None)} else 0,
        "conditions_keys": condition_keys,
        "min": min(numbers) if numbers else None,
        "max": max(numbers) if numbers else None,
        "stop_dependent": s.vocab.is_stop_dependent(key),
    }


def _scoped_rows(s: _Session, scope: _Scope) -> dict[str, list[dict[str, Any]]]:
    config_pks = {c["config_pk"] for c in scope.configs}
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in s.index.facts():
        if row["config_pk"] in config_pks:
            grouped.setdefault(row["fact_key"], []).append(row)
    return grouped


def fields(index: Index, filters: Any = None, *, fact: str | None = None, prefix: str | None = None,
           limit: int = DEFAULT_FIELDS_LIMIT, max_output_bytes: int | None = None) -> dict[str, Any]:
    """Typed fact keys available within the filtered scope.

    Without `fact`: `{"records", "fields": {key: {records, unit, value_type, bases,
    condition_variants, conditions_keys, min, max, stop_dependent}}, "returned", "more"}` sorted
    by key (optionally only keys starting with `prefix`). With `fact=KEY`: one key described for
    writing a constraint (amendment 10.12). An unknown `fact` raises UNKNOWN_FIELD.
    """
    s = _Session(index)
    budget = output.check_max_output_bytes(max_output_bytes, "max_output_bytes")
    limit = _check_int(limit, "/limit", "limit", 1, MAX_FIELDS_LIMIT, DEFAULT_FIELDS_LIMIT)
    warnings: list[dict[str, Any]] = []
    normalized = _normalize_filters(s, filters, "/filters", warnings)
    scope = _filter_scope(s, normalized)
    if fact is not None:
        key = s.require_fact(fact, "/fact")
        if key not in s.fact_keys():
            raise _invalid(f"{fact} is a path inside fact {s.known_prefix(fact)}; describe that key", "/fact",
                           did_you_mean=[s.known_prefix(fact)])
        rows = _scoped_rows(s, scope).get(key, [])
        result = {"records": len(scope.record_pks), "fact": key, **_fact_description(s, key, rows)}
        return output.finalize(_envelope(s, {"result": result}, warnings), budget)
    grouped = _scoped_rows(s, scope)
    keys = sorted(k for k in grouped if prefix is None or k.startswith(prefix))
    out = {key: _field_summary(s, key, grouped[key]) for key in keys[:limit]}
    result = {"records": len(scope.record_pks), "fields": out, "returned": len(out),
              "more": max(0, len(keys) - limit)}
    return output.finalize(_envelope(s, {"result": result}, warnings), budget)


def _fact_description(s: _Session, key: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """`fields --fact KEY` (amendment 10.12)."""
    summary = _field_summary(s, key, rows) if rows else {"records": 0}
    value_type = summary.get("value_type")
    types = value_type if isinstance(value_type, list) else ([value_type] if value_type else [])
    ops: list[str] = []
    for kind in types:
        ops.extend(op for op in _OPS_BY_TYPE.get(kind, []) if op not in ops)
    if key.endswith("weights") and "proportional_weights" not in ops:
        ops.append("proportional_weights")
    value_paths = sorted({p for r in rows if r["value_paths"] for p in json.loads(r["value_paths"])})
    forms = sorted({r["constraint_form"] for r in rows if r["constraint_form"]})
    variants = Counter(_variant(r) for r in rows)
    ordered_variants = sorted(variants.items(), key=lambda item: (-item[1], str(item[0])))
    variant_list = []
    for (conditions, qualifiers, grid), count in ordered_variants[:MAX_VARIANTS]:
        item: dict[str, Any] = {"rows": count}
        if conditions is not None:
            item["conditions"] = json.loads(conditions)
        if qualifiers is not None:
            item["qualifiers"] = json.loads(qualifiers)
        if grid is not None:
            item["sampled_field_fractions"] = json.loads(grid)
        variant_list.append(item)
    description: dict[str, Any] = {
        **summary,
        "value_paths": value_paths,
        "applicable_ops": ops,
        "eligible_for": sorted({r["eligible_for"] for r in rows if r["eligible_for"]}),
        "constraint_form": [json.loads(f) for f in forms][:3],
        "variants": variant_list,
        "more_variants": max(0, len(ordered_variants) - MAX_VARIANTS),
        "conditions_text_examples": sorted({_short_text(r["conditions_text"], 120) for r in rows
                                            if r["conditions_text"]})[:3],
    }
    description.update(_example(s, key, rows, ordered_variants))
    return description


def _example(s: _Session, key: str, rows: list[dict[str, Any]],
             ordered_variants: list[tuple[tuple[str | None, str | None, str | None], int]]) -> dict[str, Any]:
    """One ready-to-run example constraint (or a discovery filter for discovery-only facts)."""
    if not rows:
        return {"example_constraint": None}
    conditions_text, qualifiers_text, grid_text = ordered_variants[0][0]
    variant_rows = [r for r in rows if _variant(r) == ordered_variants[0][0]]
    row = variant_rows[0]
    kind = _row_type(row)
    example: dict[str, Any] = {"fact": key}
    if row["eligible_for"] == "DISCOVERY_ONLY":
        lo = min((r["value_min"] for r in rows if r["value_min"] is not None), default=None)
        hi = max((r["value_max"] for r in rows if r["value_max"] is not None), default=None)
        out: dict[str, Any] = {"example_constraint": None,
                               "note": "discovery only: it never satisfies a hard constraint"}
        if key == "spectrum.sampling_span_nm" and lo is not None and hi is not None:
            out["example_filter"] = {"filters": {"spectral_span": [math.floor(lo), math.ceil(hi)]}}
        elif lo is not None or hi is not None:
            out["example_filter"] = {"filters": {"ranges": [{"fact": key, "min": lo, "max": hi}]}}
        return out
    paths = json.loads(row["value_paths"]) if row["value_paths"] else []
    note: str | None = None
    if kind == "number":
        numbers = sorted(r["value_num"] for r in variant_rows if r["value_num"] is not None)
        example.update({"op": "between_inclusive", "value": [numbers[0], numbers[-1]]})
    elif kind == "interval":
        if row["metric"] is not None and paths:
            high = max((r["value_max"] for r in variant_rows if r["value_max"] is not None), default=0)
            upper = next((p for p in paths if p.startswith("max_")), paths[-1])
            example.update({"path": upper, "op": "lte", "value": high})
        else:
            lo, hi = row["value_min"], row["value_max"]
            example.update({"op": "covers_interval",
                            "value": [lo, hi] if lo is not None and hi is not None else [450, 650]})
            if row["state"] is not None and row["state"] not in ("CONFIRMED",):
                note = (f"state {row['state']}: covers_interval is UNCONFIRMED (COVERAGE_UNCONFIRMED) until "
                        "the coverage is confirmed")
    elif kind == "none":
        example.update({"path": paths[-1] if paths else "value", "op": "gte", "value": 0})
    elif kind == "list":
        value = public_fact_value(json.loads(row["value_json"]))
        example.update({"op": "includes_all", "value": value[:2]})
    elif kind in ("string", "bool"):
        example.update({"op": "eq", "value": json.loads(row["value_json"])})
    else:
        study = json.loads(row["value_json"])
        if isinstance(study.get("methods"), list):
            example.update({"path": "methods", "op": "has_sourced_method"})
        elif isinstance(study.get("source"), dict):
            example.update({"path": "source", "op": "has_source"})
        else:
            example.update({"path": "status", "op": "eq", "value": study.get("status")})
    if conditions_text is not None:
        example["conditions"] = json.loads(conditions_text)
    if qualifiers_text is not None and row["metric"] is not None and len(rows) > len({r["config_pk"] for r in rows}):
        example["select"] = json.loads(qualifiers_text)
    if grid_text is not None:
        grid = json.loads(grid_text)
        if isinstance(grid, list) and grid and all(_is_number(v) for v in grid):
            example["required_sampled_field_fractions"] = grid
    out = {"example_constraint": example}
    if note:
        out["note"] = note
    return out


# ---------------------------------------------------------------------------------------------
# compare (§6.7)


def _compare_cell(rows: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    if not rows:
        return None
    return _summary_entry(rows, full_basis=False)


def compare(index: Index, refs: Sequence[str], fields: Sequence[str] | None = None, *,
            max_output_bytes: int | None = None) -> dict[str, Any]:
    """Compare up to 10 records or configurations (`record_id` or `record_id@configuration_id`).

    Result: `{"candidates": [{ref, record_id, revision, configuration_id, title}], "rows": [{fact,
    values: [cell | null per candidate], comparable, differs_by?, notes?}]}`. A row is comparable when
    every present value is single-valued with the same unit, structured conditions, qualifiers and
    sampling grid (JSON value equality; a missing grid differs from any grid); otherwise
    `differs_by` names what differs and the cells carry their conditions, and each cell shows its
    `sampled_field_fractions` (null without one) when the grids differ. Rows default to the union of
    the candidates' default summary facts.
    """
    s = _Session(index)
    budget = output.check_max_output_bytes(max_output_bytes, "max_output_bytes")
    ref_list = _string_list(list(refs) if refs is not None else None, "/refs", "refs", max_items=MAX_COMPARE_REFS)
    if len(set(ref_list)) != len(ref_list):
        raise _invalid("refs must be distinct", "/refs")
    candidates: list[dict[str, Any]] = []
    configs: list[dict[str, Any]] = []
    seen: dict[tuple[Any, Any], int] = {}
    for i, ref in enumerate(ref_list):
        record_id, at, configuration_id = ref.partition("@")
        if at and not configuration_id:
            raise _invalid(f"ref {_short_text(ref)!r} has an empty configuration id after '@'",
                           _ptr("/refs", i), "use record_id or record_id@configuration_id")
        try:
            row = s.index.get_record(record_id)
            config = s.index.configuration(row["record_pk"], configuration_id or None)
        except LensError as error:
            error.details["pointer"] = _ptr("/refs", i)
            raise
        pair = (row["record_pk"], config["config_pk"])
        if pair in seen:
            raise _invalid(f"refs {_short_text(ref_list[seen[pair]])!r} and {_short_text(ref)!r} name the "
                           "same configuration", _ptr("/refs", i), "refs must be distinct configurations",
                           configuration_id=config["configuration_id"])
        seen[pair] = i
        configs.append(config)
        candidates.append({"ref": ref, "record_id": row["record_id"], "revision": row["revision"],
                           "configuration_id": config["configuration_id"], "title": row["title"]})
    if fields is not None:
        keys = _string_list(list(fields), "/fields", "fields", max_items=MAX_COMPARE_FIELDS)
        for i, key in enumerate(keys):
            s.require_fact(key, _ptr("/fields", i))
        keys = list(dict.fromkeys(keys))
    else:
        keys = sorted({k for c in configs for k in _default_summary_keys(s.config_facts(c["config_pk"]))})
    rows_out = []
    for key in keys:
        per = [s.config_facts(c["config_pk"]).get(key) or ([r for k, rs in s.config_facts(c["config_pk"]).items()
                                                            if k.startswith(key + ".") for r in rs] or None)
               for c in configs]
        cells = [_compare_cell(rows) for rows in per]
        present = [(cell, rows) for cell, rows in zip(cells, per) if rows]
        differs: list[str] = []
        if any(len(rows) > 1 for _, rows in present):
            differs.append("multi_valued")
        unit_sets = {tuple(sorted({r["unit"] for r in rows if r["unit"] is not None})) for _, rows in present}
        if len(unit_sets - {()}) > 1:
            differs.append("unit")
        singles = [(cell, rows[0]) for cell, rows in present if len(rows) == 1]
        variants = [(cell, _variant(row)) for cell, row in singles]
        labels = ("conditions", "qualifiers", "sampled_field_fractions")
        for i, label in enumerate(labels):
            if len({variant[i] for _, variant in variants}) > 1:
                differs.append(label)
        show_context = "conditions" in differs or "qualifiers" in differs
        for cell, (conditions, qualifiers, grid) in variants:
            if show_context and conditions is not None:
                cell["conditions"] = json.loads(conditions)
            if show_context and qualifiers is not None:
                cell["qualifiers"] = json.loads(qualifiers)
            if "sampled_field_fractions" in differs:
                cell["sampled_field_fractions"] = None if grid is None else json.loads(grid)
        row_out: dict[str, Any] = {"fact": key, "values": cells, "comparable": not differs}
        if differs:
            row_out["differs_by"] = differs
        missing = [i for i, rows in enumerate(per) if not rows]
        if missing:
            row_out["missing"] = missing
        texts = {row["conditions_text"] for _, row in singles}
        if len(texts) > 1:
            row_out["notes"] = ["conditions_text differs (prose conditions, information only)"]
        rows_out.append(row_out)
    result = {"candidates": candidates, "rows": rows_out}
    return output.finalize(_envelope(s, {"result": result}, []), budget)


# ---------------------------------------------------------------------------------------------
# inspect (§6.7)


def _fact_object(view: dict[str, Any], row: dict[str, Any]) -> Any:
    """The view JSON a fact row describes: the metric entry, the member object, or the scalar."""
    if row["metric"] is not None:
        entry_path = re.match(r"^(performance\[\d+\])", row["json_path"])
        return _at_path(view, entry_path.group(1)) if entry_path else _at_path(view, row["json_path"])
    form = json.loads(row["constraint_form"]) if row["constraint_form"] else {}
    scope = form.get("scope") or []
    path = form.get("path")
    if path in ("", None) or _VALUE_MEMBER.match(path or ""):
        return _at_path(view, ".".join(scope))
    return _at_path(view, ".".join(list(scope) + path.split(".")))


def _fact_full(view: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = {"json_path": row["json_path"], "value": public_fact_value(_fact_object(view, row)),
                             "unit": row["unit"], "basis": row["basis"], "basis_class": row["basis_class"]}
    for name in ("definition", "conditions_text", "eligible_for", "state", "system_aperture"):
        if row[name] is not None:
            entry[name] = row[name]
    if row["conditions_json"] is not None:
        entry["conditions"] = public_fact_value(json.loads(row["conditions_json"]))
    if row["qualifiers_json"] is not None:
        entry["qualifiers"] = public_fact_value(json.loads(row["qualifiers_json"]))
    if row["sampled_field_fractions_json"] is not None:
        entry["sampled_field_fractions"] = json.loads(row["sampled_field_fractions_json"])
    return entry


def _evidence_path(path: Any) -> str | None:
    """The normalized form of an evidence path (the raw string when it is not corpus-relative)."""
    if not isinstance(path, str):
        return None
    return corpus_relative_path(path) or path


@dataclass
class _EvidenceScope:
    """The evidence of one record as seen by the selected configuration(s) (all of them when no
    configuration is named).

    A configuration *references* a (path, section) pair when a source reference in its view names
    it. A pair referenced only by non-selected configurations does not apply. Applicable pairs are
    the other sections of the verified files that a selected view references, and of the
    record-level `collection.evidence_extract` unless only non-selected configurations reference
    that file.
    """

    selected: set[int]
    """Ordinals of the selected configurations."""
    configuration_ids: dict[int, str | None]
    pair_refs: dict[tuple[str | None, str | None], set[int]] = field(default_factory=dict)
    file_refs: dict[str | None, set[int]] = field(default_factory=dict)
    reference_configs: dict[tuple[str | None, str | None, str | None], set[int]] = field(default_factory=dict)
    documents: dict[str, tuple[str, Any]] = field(default_factory=dict)
    """Candidate files: path -> (sha256, parsed JSON of the verified bytes)."""
    damaged: dict[str, str] = field(default_factory=dict)
    """Candidate files the index no longer holds intact: path -> live status. Their bytes are
    never parsed or shown."""

    def excluded_pair(self, path: str | None, section: str | None) -> bool:
        refs = self.pair_refs.get((path, section))
        return bool(refs) and not refs & self.selected

    def excluded_file(self, path: str | None) -> bool:
        refs = self.file_refs.get(path)
        return bool(refs) and not refs & self.selected

    def lists(self, ref: dict[str, Any]) -> bool:
        """Whether an evidence_refs row belongs in the scoped listing."""
        location = ref["field"]
        path = _evidence_path(ref["path"])
        if location == "collection.evidence_extract":
            return not self.excluded_file(path)
        if location == NOTICE_FIELD:
            return False
        entry = _ENTRY_REF_FIELD.match(location)
        ordinals = self.selected if entry is None else self.selected & {int(entry.group(1))}
        referring = self.reference_configs.get((path, ref["section"], ref["sha256"]))
        return bool(referring and ordinals & referring)

    def sections(self, path: str) -> list[str]:
        """The applicable section names of a candidate file, sorted."""
        document = self.documents[path][1]
        content = document.get("sections") if isinstance(document, dict) else None
        if not isinstance(content, dict):
            return []
        return sorted(name for name in content if not self.excluded_pair(path, name))

    def by_section(self) -> dict[str, list[str]]:
        """Every applicable pair, as section name -> paths."""
        out: dict[str, list[str]] = {}
        for path in sorted(self.documents):
            for name in self.sections(path):
                out.setdefault(name, []).append(path)
        return out

    def referencing(self, path: str, section: str | None = None) -> list[str | None]:
        """Ids of the configurations referencing a file (or one of its sections), in ordinal order."""
        refs = self.file_refs.get(path) if section is None else self.pair_refs.get((path, section))
        return [self.configuration_ids[ordinal] for ordinal in sorted(refs or ())]


def _evidence_scope(s: _Session, configs: list[dict[str, Any]], rows: list[dict[str, Any]],
                    selected_ordinal: int | None, project_sha: str, revision: int) -> _EvidenceScope:
    selected = {selected_ordinal} if selected_ordinal is not None else {c["ordinal"] for c in configs}
    scope = _EvidenceScope(selected, {c["ordinal"]: c["configuration_id"] for c in configs})
    candidates: dict[str, set[str]] = {}
    for item in configs:
        ordinal = item["ordinal"]
        for _, ref in iter_source_refs(item["view"]):
            path = _evidence_path(ref.get("path"))
            section = ref.get("section") if isinstance(ref.get("section"), str) else None
            digest = ref.get("sha256") if isinstance(ref.get("sha256"), str) else None
            scope.pair_refs.setdefault((path, section), set()).add(ordinal)
            scope.file_refs.setdefault(path, set()).add(ordinal)
            scope.reference_configs.setdefault((path, section, digest), set()).add(ordinal)
            if ordinal in selected and path is not None and digest is not None:
                candidates.setdefault(path, set()).add(digest)
    # A rejected extract contributes no file, even if another record references the same bytes.
    for ref in rows:
        if ref["field"] == "collection.evidence_extract" and ref["build_status"] in ("RESOLVED", "SECTION_MISSING"):
            path = _evidence_path(ref["path"])
            if path is not None and ref["sha256"] is not None and not scope.excluded_file(path):
                candidates.setdefault(path, set()).add(ref["sha256"])
    # (path, sha256) pairs that reached a stored file at build: failing now means damage. A pair
    # the build rejected (--allow-unresolved-evidence) is merely unresolved.
    stored_at_build = {(_evidence_path(ref["path"]), ref["sha256"]) for ref in rows
                       if ref["build_status"] in ("RESOLVED", "SECTION_MISSING") and ref["sha256"] is not None}
    for path in sorted(candidates):
        for sha in sorted(candidates[path]):
            status, content = s.index.evidence_state(path, sha)
            if content is None:
                if s.index.evidence_damaged(path):
                    scope.damaged[path] = "HASH_MISMATCH"
                elif (path, sha) in stored_at_build:
                    scope.damaged[path] = status
                continue
            try:
                document = json.loads(content.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, RecursionError):
                continue
            if isinstance(document, dict) and any(
                analysis_applies(document, project_sha, scope.configuration_ids[ordinal], revision)
                for ordinal in selected
            ):
                scope.documents[path] = (sha, document)
    return scope


def _damaged_evidence(index: Index, record_id: str, damaged: dict[str, str]) -> LensError:
    """INDEX_UNAVAILABLE for evidence the index no longer holds intact (nothing of it is echoed)."""
    path, status = sorted(damaged.items())[0]
    problem = f"damaged: stored evidence {path} does not verify against its sha256 ({status})"
    return LensError(
        "INDEX_UNAVAILABLE", f"the index {index.path} is damaged: stored evidence of record {record_id!r} "
        "does not verify against its sha256",
        hint="Rebuild the index with `os-optics-lens index build --corpus <dir> --out <file>`.",
        details={"tried": [{"source": index.source, "path": str(index.path), "problem": problem}],
                 "path": str(index.path), "record_id": record_id,
                 "evidence": [{"path": name, "status": state} for name, state in sorted(damaged.items())]})


def _select_evidence(scope: _EvidenceScope, names: list[str], record_id: str) -> dict[str, Any]:
    """The requested evidence sections keyed by file path. A name is a section name (which must
    identify one applicable file), `path#section`, or `all` (every applicable pair)."""
    available = scope.by_section()
    if "all" in names:
        wanted = [(path, name) for name, paths in available.items() for path in paths]
    else:
        wanted = []
        for i, name in enumerate(names):
            paths = available.get(name)
            if paths is None and "#" in name:
                raw_path, _, section = name.rpartition("#")
                path = _evidence_path(raw_path)
                if path in available.get(section, ()):
                    wanted.append((path, section))
                    continue
            if paths is None:
                raise LensError(
                    "EVIDENCE_SECTION_NOT_FOUND",
                    f"record {record_id!r} has no evidence section {_short_text(name)!r} in the selected "
                    "configuration scope",
                    hint="sections: " + (", ".join(sorted(available)) or "none"),
                    details={"pointer": _ptr("/evidence", i), "section": _short_text(name),
                             "available": sorted(available)})
            if len(paths) > 1:
                raise LensError(
                    "EVIDENCE_AMBIGUOUS",
                    f"evidence section {name!r} names {len(paths)} different files of record {record_id!r}",
                    hint=f"Select one as path#section (e.g. --evidence {paths[0]}#{name}), or narrow with "
                         "--configuration.",
                    details={"pointer": _ptr("/evidence", i), "section": name,
                             "candidates": [{"path": path, "configurations": scope.referencing(path, name)}
                                            for path in paths]})
            wanted.append((paths[0], name))
    out: dict[str, Any] = {}
    for path, name in sorted(set(wanted)):
        sha, document = scope.documents[path]
        entry = out.setdefault(path, {"sha256": sha, "sections": {}})
        entry["sections"][name] = document["sections"][name]
    return out


def _objects(value: Any) -> list[dict[str, Any]]:
    """The objects of a list; anything else (the Project's models are not type-checked at build)
    contributes nothing."""
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _position(item: dict[str, Any]) -> Any:
    pose = item.get("pose")
    return pose.get("position_mm") if isinstance(pose, dict) else None


def _prescription(project: Any, level: str, native_ids: list[Any]) -> dict[str, Any]:
    if not isinstance(project, dict):
        return {}
    if level == "full":
        return {name: project.get(name) for name in ("optical_models", "model_materials", "model_coatings")}
    models = []
    for model in _objects(project.get("optical_models")):
        body = model.get("model") if isinstance(model.get("model"), dict) else {}
        item: dict[str, Any] = {"model_id": model.get("model_id"), "kind": body.get("model_kind")}
        members = body.get("members")
        if isinstance(members, list):
            item["members"] = [
                {"member_id": m.get("member_id"), "model_id": m.get("model_id"), "position_mm": _position(m)}
                for m in _objects(members)]
        surfaces = body.get("surfaces")
        if isinstance(surfaces, list):
            item["surfaces"] = len(surfaces)
            materials = sorted({str(v) for surf in _objects(surfaces)
                                for k, v in surf.items() if k.endswith("material_id") and v})
            if materials:
                item["materials"] = materials
        models.append(item)
    roles = Counter(role if role is None or isinstance(role, str) else json.dumps(role, sort_keys=True)
                    for role in (c.get("optical_role") for c in _objects(project.get("components"))))
    observations = [
        {"observation_id": o.get("observation_id"), "aperture": o.get("aperture"), "position_mm": _position(o)}
        for o in _objects(project.get("observations"))]
    return {
        "optical_models": models,
        "materials": [m.get("material_id") for m in _objects(project.get("model_materials"))],
        "coatings": [c.get("coating_id") for c in _objects(project.get("model_coatings"))],
        "observation_planes": observations,
        "components": dict(sorted((str(k), v) for k, v in roles.items())),
        "native_configuration_ids": native_ids,
    }


def _license(collection: dict[str, Any]) -> dict[str, Any]:
    license_obj = collection.get("license") if isinstance(collection.get("license"), dict) else {}
    out: dict[str, Any] = {}
    for name in ("spdx", "copyright", "notice_path"):
        if license_obj.get(name) is not None:
            out[name] = license_obj[name]
    redistribution = collection.get("redistribution")
    if "notice_path" not in out and isinstance(redistribution, str):
        match = _LICENSE_PATH.search(redistribution)
        if match:
            out["notice_path"] = match.group(1)
    if isinstance(redistribution, str):
        out["redistribution"] = redistribution
    return out


def _provenance(collection: dict[str, Any]) -> dict[str, Any]:
    return {**{key: collection[key] for key in _PROVENANCE_MEMBERS if key in collection},
            "license": _license(collection)}


def _configuration_summary(config: dict[str, Any]) -> dict[str, Any]:
    return {key: config[key] for key in ("configuration_id", "label", "native", "ordinal")}


def inspect(index: Index, record_id: str, *, revision: int | None = None, configuration: str | None = None,
            fields: Sequence[str] | None = None, sections: Sequence[str] | None = None,
            evidence: Sequence[str] | None = None, prescription: str | None = None,
            provenance: bool = False, max_output_bytes: int | None = None) -> dict[str, Any]:
    """One record's identity, native Project metadata, configurations, facets and available facts.

    `fields` adds selected facts by key or prefix. `sections` adds selected Project fact sections.
    Supporting evidence is scoped to `configuration` when supplied and covers all configurations
    otherwise. `evidence` selects verified calculation sections by name, `path#section` or "all".
    `prescription` selects a native model overview ("summary") or details ("full"). `provenance`
    adds the direct source, attribution and license terms. Oversized results raise OUTPUT_LIMIT_EXCEEDED.
    """
    s = _Session(index)
    budget = output.check_max_output_bytes(max_output_bytes, "max_output_bytes")
    row = s.index.get_record(record_id, revision)
    record = row["record"]
    config = s.index.configuration(row["record_pk"], configuration)
    view = config["view"]
    project_meta = s.index.project(row["record_pk"])
    facts = s.config_facts(config["config_pk"])
    design = view.get("design") or {}
    record_facets: dict[str, list[str]] = {}
    for name in s.vocab.facet_names():
        values = sorted(s.facets_of(config, name))
        if values:
            record_facets[name] = values
    configs = s.index.configurations(row["record_pk"], with_view=True)
    refs = s.index.evidence_refs(row["record_pk"])
    selected_ordinal = config["ordinal"] if configuration is not None else None
    evidence_scope = _evidence_scope(s, configs, refs, selected_ordinal, project_meta["sha256"], row["revision"])
    grouped_refs: dict[tuple[Any, Any, Any], int] = {}
    for ref in refs:
        if not evidence_scope.lists(ref):
            continue
        grouped_refs[(ref["path"], ref["section"], ref["status"])] = grouped_refs.get(
            (ref["path"], ref["section"], ref["status"]), 0) + 1
    result: dict[str, Any] = {
        "record_id": row["record_id"], "revision": row["revision"], "title": row["title"],
        "record_schema_version": row["record_schema_version"], "record_hash": row["record_hash"],
        "collection": {"collection_id": row["collection_id"]},
        "design": {k: design.get(k) for k in ("design_family_id", "design_scope", "lens_type", "conjugate",
                                              "configuration_count") if k in design},
        "native_project": {"path": row["project_path"], "sha256": project_meta["sha256"],
                           "size_bytes": project_meta["size_bytes"], "engine_version": row["engine_version"],
                           "native_configuration_ids": project_meta["native_configuration_ids"]},
        "configuration": _configuration_summary(config),
        "configurations": [_configuration_summary(c) for c in configs],
        "facets": record_facets,
        "fact_keys": sorted(facts),
        "evidence": {
            "refs": [{"path": p, "section": sec, "status": st, "count": n}
                     for (p, sec, st), n in sorted(grouped_refs.items(), key=lambda item: tuple(str(x) for x in item[0]))],
            "sections": {path: evidence_scope.sections(path) for path in sorted(evidence_scope.documents)},
            "files": {path: {"configurations": evidence_scope.referencing(path),
                             **({"status": evidence_scope.damaged[path]} if path in evidence_scope.damaged else {})}
                      for path in sorted(set(evidence_scope.documents) | set(evidence_scope.damaged))},
        },
        "next": {
            "fields": f"os-optics-lens inspect {row['record_id']} --fields <fact_key,...>",
            "evidence": f"os-optics-lens inspect {row['record_id']} --evidence <section|path#section|all>",
            "prescription": f"os-optics-lens inspect {row['record_id']} --prescription summary",
            "provenance": f"os-optics-lens inspect {row['record_id']} --provenance",
            "project": f"os-optics-lens project {row['record_id']} --out <file>",
        },
    }
    if provenance:
        result["provenance"] = _provenance(record.get("collection") or {})
    if fields is not None:
        keys = _string_list(list(fields), "/fields", "fields")
        selected: dict[str, Any] = {}
        for i, key in enumerate(keys):
            s.reject_check(key, _ptr("/fields", i))
            matched = [k for k in sorted(facts) if k == key or k.startswith(key + ".")]
            if not matched:
                if s.is_known_fact(key) or any(k.startswith(key + ".") for k in s.fact_keys()):
                    selected[key] = None
                    continue
                raise s.unknown_field(key, _ptr("/fields", i))
            for k in matched:
                entries = [_fact_full(view, r) for r in facts[k]]
                selected[k] = {"facts": entries} if len(entries) > 1 else entries[0]
        result["fields"] = selected
    if sections is not None:
        names = _string_list(list(sections), "/sections", "sections")
        chosen: dict[str, Any] = {}
        for i, name in enumerate(names):
            if name not in view:
                close = difflib.get_close_matches(name, sorted(view), n=3, cutoff=0.5)
                raise LensError("UNKNOWN_FIELD", f"the configuration view has no section {name!r}",
                                hint="sections: " + ", ".join(sorted(view)),
                                details={"pointer": _ptr("/sections", i), "section": name, "did_you_mean": close})
            chosen[name] = public_fact_value(view[name])
        result["sections"] = chosen
    if evidence is not None:
        names = _string_list(list(evidence), "/evidence", "evidence")
        if evidence_scope.damaged:
            # A damaged file's sections are unknown, so no selection over this record is safe.
            raise _damaged_evidence(s.index, row["record_id"], evidence_scope.damaged)
        result["evidence_sections"] = _select_evidence(evidence_scope, names, row["record_id"])
    if prescription is not None:
        if prescription not in PRESCRIPTION_LEVELS:
            raise _invalid("prescription must be summary or full", "/prescription", allowed=list(PRESCRIPTION_LEVELS))
        project = json.loads(s.index.project_bytes(row["record_pk"]).decode("utf-8"))
        result["prescription"] = _prescription(project, prescription, project_meta["native_configuration_ids"])
    response = _envelope(s, {"result": result}, [])
    try:
        return output.finalize(response, budget)
    except LensError as error:
        if error.code == "OUTPUT_LIMIT_EXCEEDED":
            error.hint = ("Narrow the request: name fewer fields, sections or evidence sections, use "
                          "--prescription summary, or fetch the Project with `os-optics-lens project --out`; "
                          "max_output_bytes is at most 262144.")
        raise


# ---------------------------------------------------------------------------------------------
# project hand-off (§6.7, amendment 10.16)


def project_metadata(index: Index, record_id: str, *, revision: int | None = None) -> dict[str, Any]:
    """Metadata of a record's native Project (no I/O).

    The sha256 is over the exact LF bytes `project_bytes` returns and equals the record's
    `native_project.sha256`.
    """
    row = index.get_record(record_id, revision)
    meta = index.project(row["record_pk"])
    configs = index.configurations(row["record_pk"], with_view=False)
    return {
        "record_id": row["record_id"], "revision": row["revision"], "sha256": meta["sha256"],
        "size_bytes": meta["size_bytes"], "native_configuration_ids": meta["native_configuration_ids"],
        "engine_version": row["engine_version"],
        "configurations": [_configuration_summary(config) for config in configs],
        "next": (f"Write the exact Project bytes with `os-optics-lens project {row['record_id']} --out <file>` "
                 "(add --force to overwrite), then open, check or evaluate it with os-optics."),
    }


def project_bytes(index: Index, record_id: str, *, revision: int | None = None) -> bytes:
    """The native Project's exact LF bytes, re-verified against its sha256 (a damaged index raises
    INDEX_UNAVAILABLE before anything is written)."""
    row = index.get_record(record_id, revision)
    try:
        return index.project_bytes(row["record_pk"])
    except LensError as error:
        if error.code == "INDEX_UNAVAILABLE":
            error.details.pop("record_pk", None)
            error.details["record_id"] = row["record_id"]
        raise


def project_response(index: Index, record_id: str, *, revision: int | None = None,
                     extra: dict[str, Any] | None = None, max_output_bytes: int | None = None) -> dict[str, Any]:
    """Envelope for `project` without `--stdout`: metadata plus `extra` (e.g. the CLI's written path)."""
    s = _Session(index)
    budget = output.check_max_output_bytes(max_output_bytes, "max_output_bytes")
    result = project_metadata(index, record_id, revision=revision)
    if extra:
        result.update(extra)
    return output.finalize(_envelope(s, {"result": result}, []), budget)

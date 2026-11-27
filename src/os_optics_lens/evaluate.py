"""Pure constraint semantics over one configuration view (SPEC §6.2-§6.4, amendments 10.1-10.4,
10.6, 10.9, 10.11).

A *configuration view* is a record's JSON with one configuration's sections in place and
`view["configuration"]` attached (SPEC §2.3). A 1.0 record is its own view apart from that key.
Everything here is a pure function of the view, the constraint and an `EvalContext`; the context
injects the stop-dependent fact keys (from the vocabulary) and the evidence resolver (from the index
or, in tests, from a checkout), so this module imports nothing else from the package.

Outcomes are `MATCH`, `UNCONFIRMED` and `DOES_NOT_MATCH`. An `UNCONFIRMED` evaluation always carries
one reason code from `REASON_CODES`. Value equality everywhere is JSON value equality
(`json_equal`): `0 == 0.0`, a boolean never equals a number, lists are ordered and objects compare
key-wise.

Constraint forms accepted by `evaluate_constraint`:

* fixture form `{id, scope, predicates: [{path, op, value, tolerance?}], metric?, select?,
  conditions?, requires_blocking_stop?, required_sampled_field_fractions?,
  requires_evidence_source?}`, evaluated as written;
* shorthand `{id?, fact, op, value?, path?, tolerance?, select?, conditions?, requires_*?}`, which
  `expand_for_view` turns into the fixture form against the shape the fact has in the view being
  evaluated (amendment 10.3).
"""

from __future__ import annotations

import copy
import difflib
import json
import math
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

__all__ = [
    "MATCH",
    "UNCONFIRMED",
    "DOES_NOT_MATCH",
    "OUTCOMES",
    "OPERATORS",
    "REASON_CODES",
    "EvalContext",
    "RequestError",
    "json_equal",
    "canonical_value",
    "unit_for_name",
    "proportional_weights",
    "stop_status",
    "is_shorthand",
    "is_fixture_preference",
    "constraint_fact_key",
    "fact_present",
    "validate_constraint",
    "validate_target",
    "validate_preference",
    "validate_preferences",
    "expand_constraint",
    "expand_for_view",
    "evaluate_constraint",
    "evaluate_configuration",
    "configuration_rank",
    "target_value",
    "target_deviation",
    "preference_value",
    "preference_rank_entry",
    "ranking_conditions_equal",
]

MATCH = "MATCH"
UNCONFIRMED = "UNCONFIRMED"
DOES_NOT_MATCH = "DOES_NOT_MATCH"
OUTCOMES = (MATCH, UNCONFIRMED, DOES_NOT_MATCH)

STOP_NOT_ELIGIBLE = "STOP_NOT_ELIGIBLE"
STOP_MATCH_NOT_SUPPORTED = "STOP_MATCH_NOT_SUPPORTED"
FACT_MISSING = "FACT_MISSING"
CONDITIONS_DIFFER = "CONDITIONS_DIFFER"
AMBIGUOUS_FACTS = "AMBIGUOUS_FACTS"
DISCOVERY_ONLY = "DISCOVERY_ONLY"
SAMPLING_DIFFERS = "SAMPLING_DIFFERS"
SAMPLING_UNSPECIFIED = "SAMPLING_UNSPECIFIED"
EVIDENCE_MISSING = "EVIDENCE_MISSING"
EVIDENCE_UNRESOLVED = "EVIDENCE_UNRESOLVED"
VALUE_MISSING = "VALUE_MISSING"
VALUE_TYPE = "VALUE_TYPE"
COVERAGE_UNCONFIRMED = "COVERAGE_UNCONFIRMED"
PREFERENCE_NOT_COMPARABLE = "PREFERENCE_NOT_COMPARABLE"
COMPARISON_ONLY = "COMPARISON_ONLY"

REASON_CODES = frozenset({
    STOP_NOT_ELIGIBLE, STOP_MATCH_NOT_SUPPORTED, FACT_MISSING, CONDITIONS_DIFFER, AMBIGUOUS_FACTS,
    DISCOVERY_ONLY, SAMPLING_DIFFERS, SAMPLING_UNSPECIFIED, EVIDENCE_MISSING, EVIDENCE_UNRESOLVED,
    VALUE_MISSING, VALUE_TYPE, COVERAGE_UNCONFIRMED, PREFERENCE_NOT_COMPARABLE, COMPARISON_ONLY,
})

OPERATORS = (
    "eq", "ne", "gt", "gte", "lt", "lte", "between_inclusive", "in", "proportional_weights",
    "has_source", "has_sourced_method", "covers_interval", "includes_all",
)
_ORDER_OPS = frozenset({"gt", "gte", "lt", "lte"})
_PAIR_OPS = frozenset({"between_inclusive", "covers_interval"})
_SOURCE_OPS = frozenset({"has_source", "has_sourced_method"})
# Default predicate path of a source operator on an object fact named without a member.
_SOURCE_DEFAULT_PATH = {"has_source": "source", "has_sourced_method": "methods"}

RESOLVED = "RESOLVED"
MISSING = "MISSING"
EVIDENCE_STATUSES = frozenset({
    RESOLVED, MISSING, "FILE_MISSING", "HASH_MISMATCH", "SECTION_MISSING", "MALFORMED",
    "PATH_ESCAPES",
})

_DISCOVERY_MARK = "DISCOVERY_ONLY"
_COVERAGE_KEY = ("spectrum", "continuous_coverage_nm")
_CONFIRMED_COVERAGE_STATES = frozenset({"CONFIRMED"})
_VALUE_MEMBER = re.compile(r"^value(_[a-z_]+)?$")
_FACT_SYNTAX = re.compile(r"^[^.\s]+(\.[^.\s]+)*$")
_NOT_FACTS = frozenset({
    "catalog_comparison", "catalog_comparisons", "engine_check", "prescription_dump_check",
    "property_list_comparison",
})
"""Provenance checks (docs/RECORD_FORMAT.md): agreement/comparison data kept beside the values,
never a value. Mirrors `fact_extraction.check_members` in vocabulary.json."""
_SAMPLING_MEMBER = "sampled_field_fractions"
_UNPINNED_LIMIT = 16
_UNPINNED_TEXT_CHARS = 80

_FIXTURE_KEYS = frozenset({
    "id", "scope", "predicates", "metric", "select", "conditions", "requires_blocking_stop",
    "required_sampled_field_fractions", "requires_evidence_source",
})
_SHORTHAND_KEYS = frozenset({
    "id", "fact", "op", "value", "path", "tolerance", "select", "conditions",
    "requires_blocking_stop", "required_sampled_field_fractions", "requires_evidence_source",
})
_SHORTHAND_PREDICATE_KEYS = frozenset({"op", "value", "path", "tolerance"})
_PREDICATE_KEYS = frozenset({"path", "op", "value", "tolerance"})
_TARGET_KEYS = frozenset({
    "id", "fact", "target", "op", "tolerance", "relative_tolerance", "conditions", "weight",
    "select", "path", "required_sampled_field_fractions",
})
_PREFERENCE_KEYS = frozenset({
    "id", "fact", "direction", "conditions", "select", "path", "required_sampled_field_fractions",
})
_FIXTURE_PREFERENCE_KEYS = frozenset({
    "id", "scope", "metric", "direction", "spherical_element_weight", "aspheric_element_weight",
    "scope_of_cost",
})
_PREFERENCE_PIN_TARGETS = {"scope_of_cost": "scope"}
_DIRECTIONS = ("ascending", "descending")


# ---------------------------------------------------------------------------------------------
# Context and errors


def _unresolving_resolver(source: object) -> dict[str, Any]:
    """Default resolver: no evidence is available, so a present source never resolves."""
    return {"status": MISSING if source is None else "FILE_MISSING"}


@dataclass(frozen=True)
class EvalContext:
    """What evaluation needs beyond the view.

    `stop_dependent` and `stop_independent` hold dotted fact keys (`"performance.mtf"`,
    `"aperture.stop_opening_diameter_mm"`). With `aperture_default_stop_dependent`, every
    `aperture.<member>` not listed in `stop_independent` is stop-dependent (amendment 10.1);
    `stop_dependent_sections` extends that default to further sections (vocabulary.json
    `stop_dependent.sections`).
    `resolve_source(source)` implements amendment 10.4 and returns a mapping whose `status` is one of
    `RESOLVED`, `MISSING` (no source object), `FILE_MISSING`, `HASH_MISMATCH`, `SECTION_MISSING`,
    `MALFORMED` or `PATH_ESCAPES`.
    """

    stop_dependent: frozenset[str] = frozenset()
    stop_independent: frozenset[str] = frozenset()
    aperture_default_stop_dependent: bool = True
    resolve_source: Callable[[object], Mapping[str, Any]] = field(default=_unresolving_resolver)
    stop_dependent_sections: frozenset[str] = frozenset()

    def is_stop_dependent(self, key: str) -> bool:
        """Whether the dotted fact key (exactly `section.member` or `performance.<metric>`) names a
        stop-dependent fact. Members of `stop_dependent_sections` (plus `aperture` when
        `aperture_default_stop_dependent`) are stop-dependent unless listed in `stop_independent`.
        Longer paths are judged by the evaluator through their prefixes."""
        if key in self.stop_dependent:
            return True
        segments = key.split(".")
        if len(segments) != 2:
            return False
        section = segments[0]
        if section in self.stop_dependent_sections or (
                self.aperture_default_stop_dependent and section == "aperture"):
            return key not in self.stop_independent
        return False

    def source_status(self, source: object) -> str:
        """The evidence status of a source object (`MISSING` when there is none)."""
        if source is None:
            return MISSING
        result = self.resolve_source(source)
        status = result.get("status") if isinstance(result, Mapping) else None
        return status if isinstance(status, str) else "MALFORMED"


class RequestError(ValueError):
    """A request that cannot be evaluated as written.

    `code` is `INVALID_REQUEST` or `UNSUPPORTED_OPERATOR`; `pointer` is a JSON pointer into the
    request (also present as `details["pointer"]`); `hint` says how to fix it. The query layer maps
    this onto its error envelope unchanged.
    """

    def __init__(self, code: str, message: str, pointer: str | None = None,
                 hint: str | None = None, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.pointer = pointer
        self.hint = hint
        self.details: dict[str, Any] = dict(details or {})
        if pointer is not None:
            self.details.setdefault("pointer", pointer)

    def __repr__(self) -> str:
        return f"RequestError({self.code!r}, {self.message!r}, pointer={self.pointer!r})"


# ---------------------------------------------------------------------------------------------
# Value helpers


def _is_number(value: object) -> bool:
    """A finite JSON number (booleans are not numbers). An integer too large for a double is not
    finite as a double, so it is not a number here (and never raises OverflowError)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def json_equal(a: object, b: object) -> bool:
    """JSON value equality: numbers by value (`0 == 0.0`), booleans only equal booleans, lists
    element-wise in order, objects key-wise. NaN equals nothing."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(json_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(json_equal(a[k], b[k]) for k in a)
    return False


def _finite_or_none(compute: Callable[[], Any]) -> float | None:
    """`float(compute())` when it is a finite double, else None. Arithmetic on accepted finite
    inputs can still overflow (a tiny limit, bounds near +/-1.7e308, big integers); an
    unrepresentable result is reported as null, never as inf or an exception."""
    try:
        result = float(compute())
    except (OverflowError, ZeroDivisionError):
        return None
    return result if math.isfinite(result) else None


def canonical_value(value: object) -> object:
    """The value with integral floats normalized to int, so equal JSON values serialize alike."""
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    if isinstance(value, list):
        return [canonical_value(item) for item in value]
    if isinstance(value, dict):
        return {key: canonical_value(item) for key, item in value.items()}
    return value


def public_fact_value(value: object) -> Any:
    """Project fact data without references reserved for explicit evidence checks."""
    if isinstance(value, dict):
        return {key: public_fact_value(item) for key, item in value.items() if key != "source"}
    if isinstance(value, list):
        return [public_fact_value(item) for item in value]
    return value


def _canonical_text(value: object) -> str:
    return json.dumps(canonical_value(value), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _at(data: object, path: Sequence[str]) -> object:
    """The member at `path` through nested objects, or None (the reference `at`)."""
    for key in path:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _split(path: str) -> list[str]:
    return path.split(".") if path else []


def unit_for_name(name: str) -> str | None:
    """Unit implied by a member name (amendment 10.8): a name containing `_fraction_of_` is a
    ratio ("1"); else strip `_from_<word>`, then the longest suffix wins (`_lp_mm`, `_percent`,
    `_deg`, `_um`, `_nm`, `_mm`); names ending in f_number, numerical_aperture, `_count`,
    `_fraction` or `_weight` are dimensionless ("1")."""
    if "_fraction_of_" in name:
        return "1"
    stem = re.sub(r"_from_[a-z0-9]+$", "", name)
    for suffix, unit in (("_lp_mm", "lp/mm"), ("_percent", "percent"), ("_deg", "deg"),
                         ("_um", "um"), ("_nm", "nm"), ("_mm", "mm")):
        if stem.endswith(suffix):
            return unit
    if stem.endswith(("f_number", "numerical_aperture", "_count", "_fraction", "_weight")):
        return "1"
    return None


def _unit_stem(name: str) -> str:
    for suffix in ("_lp_mm", "_percent", "_deg", "_um", "_nm", "_mm"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def proportional_weights(actual: object, expected: object) -> bool:
    """Whether two weight vectors are proportional (the reference rule): same non-zero length,
    finite non-negative numbers, non-zero maxima, ratios to the maximum equal within 1e-12."""
    if not isinstance(actual, list) or not actual or not isinstance(expected, list):
        return False
    if len(actual) != len(expected):
        return False
    if any(not _is_number(w) or w < 0 for w in (*actual, *expected)):
        return False
    actual_scale, expected_scale = max(actual), max(expected)
    return bool(actual_scale and expected_scale) and all(
        math.isclose(a / actual_scale, b / expected_scale, rel_tol=1e-12)
        for a, b in zip(actual, expected)
    )


def _member_equal(key: str, actual: object, expected: object) -> bool:
    """One requested spectrum member against the stored one: incident weights compare by ratio
    (over the same ordered wavelengths, which are compared as their own member), every other
    member by JSON equality."""
    if key == "incident_weights" and expected is not None:
        return proportional_weights(actual, expected)
    return json_equal(actual, expected)


def _difference(own: Mapping[str, Any], member: str, expected: object) -> dict[str, Any]:
    """The difference detail of one requested member: an absent member says `present: false`,
    a stored one (possibly an explicit null) shows its `actual` value."""
    if member not in own:
        return {"expected": expected, "present": False}
    return {"expected": expected, "actual": own[member]}


def _condition_differences(own: object, key: str, expected: object) -> list[tuple[str, dict[str, Any]]]:
    """The mismatches of one requested condition against a fact's `conditions` object; empty when
    it matches. A requested member must be PRESENT: an absent one never equals a requested null.
    A dict-valued `spectrum` request is compared member by member (members it does not name stay
    unpinned); mismatches are then listed as `spectrum.<member>`."""
    if not isinstance(own, dict) or key not in own:
        return [(key, {"expected": expected, "present": False})]
    actual = public_fact_value(own[key])
    if key == "spectrum" and isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [(key, {"expected": expected, "actual": actual})]
        return [(f"{key}.{member}", _difference(actual, member, want))
                for member, want in expected.items()
                if member not in actual or not _member_equal(member, actual[member], want)]
    return [] if json_equal(actual, expected) else [(key, {"expected": expected, "actual": actual})]


def _conditions_match(own: object, conditions: Mapping[str, Any]) -> bool:
    return isinstance(own, dict) and not any(
        _condition_differences(own, key, expected) for key, expected in conditions.items())


def _short(value: object, limit: int = _UNPINNED_TEXT_CHARS) -> object:
    """The value itself if its compact JSON fits in `limit` characters, else a truncated string."""
    text = value if isinstance(value, str) else _canonical_text(value)
    if len(text) <= limit:
        return value
    return text[: limit - 3] + "..."


def stop_status(view: Mapping[str, Any]) -> dict[str, Any]:
    """The view's stop gate: `eligible` (stop blocks light and the record is eligible for
    stop-dependent evaluation), `satisfies` (it may claim a stop-dependent match), plus the
    recorded `state` and `satisfaction_basis`."""
    validity = _at(view, ["design", "aperture_validity"])
    if not isinstance(validity, dict):
        validity = {}
    return {
        "eligible": validity.get("stop_blocks_light") is True
        and validity.get("eligible_for_stop_dependent_evaluation") is True,
        "satisfies": validity.get("satisfies_stop_dependent_constraints") is not False,
        "state": validity.get("state"),
        "satisfaction_basis": validity.get("satisfaction_basis"),
    }


def _stop_details(stop: Mapping[str, Any]) -> dict[str, Any]:
    return {"state": stop["state"], "satisfaction_basis": stop["satisfaction_basis"]}


def _prefix_stop_dependent(segments: Sequence[str], ctx: EvalContext) -> str | None:
    """The shortest prefix of `segments` naming a stop-dependent fact, or None."""
    for end in range(1, len(segments) + 1):
        key = ".".join(segments[:end])
        if ctx.is_stop_dependent(key):
            return key
    return None


def _contained_stop_dependent(full: Sequence[str], node: object, ctx: EvalContext) -> str | None:
    """A stop-dependent fact *inside* the value a predicate compares as a whole, or None.

    A predicate whose path ends at a section (`aperture`, or the `performance` list) compares every
    member under it, so it reaches each stop-dependent fact the section holds and is gated like a
    predicate on that fact (amendment 10.2 (i) applied to the whole object). Deeper paths are judged
    through their prefixes by `_prefix_stop_dependent`; explicitly listed keys longer than the path
    are matched too."""
    prefix = ".".join(full)
    if len(full) == 1:
        names: list[str] = []
        if isinstance(node, dict):
            names = [str(key) for key in node]
        elif isinstance(node, list):
            names = [entry["metric"] for entry in node
                     if isinstance(entry, dict) and isinstance(entry.get("metric"), str)]
        for name in names:
            key = f"{prefix}.{name}"
            if ctx.is_stop_dependent(key):
                return key
    for key in sorted(ctx.stop_dependent):
        if key.startswith(prefix + ".") and _has_path(node, key[len(prefix) + 1:].split(".")):
            return key
    return None


def _not_fact_hit(segments: Sequence[str], base: str = "") -> dict[str, Any] | None:
    """The first path member that is a related check (`property_list_comparison`, `engine_check`):
    such objects are kept for comparison only and are never values (SPEC extraction rule 1)."""
    walked = base
    for segment in segments:
        walked = f"{walked}.{segment}" if walked else segment
        if segment in _NOT_FACTS:
            return {"json_path": walked, "member": segment,
                    "note": f"{segment} is a provenance check (comparison only), never a value; it "
                            "cannot satisfy or refute a constraint",
                    "hint": "constrain the fact itself (see `fields`)"}
    return None


def _source_path_hit(segments: Sequence[str], base: str = "") -> dict[str, Any] | None:
    if "source" not in segments:
        return None
    path = ".".join(segments[:segments.index("source") + 1])
    return {"json_path": f"{base}.{path}" if base else path, "member": "source",
            "hint": "Use has_source, has_sourced_method or requires_evidence_source to check supporting evidence."}


def _contained_hit(node: object, base: str) -> tuple[str, dict[str, Any]] | None:
    """A comparison-only check or a discovery-only object nested *inside* `node` (the value a
    predicate compares as a whole): comparing the whole object would let that data decide."""
    stack: list[tuple[str, object]] = [(base, node)]
    while stack:
        path, item = stack.pop()
        if isinstance(item, dict):
            children = [(f"{path}.{key}" if path else str(key), key, value) for key, value in item.items()]
        elif isinstance(item, list):
            children = [(f"{path}[{index}]", None, value) for index, value in enumerate(item)]
        else:
            continue
        for child_path, key, value in reversed(children):
            if key in _NOT_FACTS:
                hit = _not_fact_hit([str(key)]) or {}
                hit["json_path"] = child_path
                return COMPARISON_ONLY, hit
            discovery = _discovery_hit(child_path, value)
            if discovery:
                return DISCOVERY_ONLY, discovery
            stack.append((child_path, value))
    return None


# ---------------------------------------------------------------------------------------------
# Constraint shape helpers


def is_shorthand(constraint: Mapping[str, Any]) -> bool:
    """A shorthand constraint names `fact` and has not been expanded to a `scope`."""
    return "fact" in constraint and "scope" not in constraint


def is_fixture_preference(preference: Mapping[str, Any]) -> bool:
    """The fixture `ranking_preference` form is recognized by `scope` plus `metric`."""
    return "scope" in preference and "metric" in preference


def constraint_fact_key(constraint: Mapping[str, Any]) -> str:
    """The dotted fact key a constraint addresses: `fact` for shorthand, else scope (+ metric)."""
    if is_shorthand(constraint):
        return str(constraint["fact"])
    segments = list(constraint.get("scope") or [])
    if constraint.get("metric") is not None:
        segments.append(str(constraint["metric"]))
    return ".".join(segments)


def _metric_entries(view: object, scope: Sequence[str], metric: str,
                    select: Mapping[str, Any] | None = None) -> list[tuple[int, dict[str, Any]]]:
    container = _at(view, scope)
    if not isinstance(container, list):
        return []
    entries = [(i, e) for i, e in enumerate(container)
               if isinstance(e, dict) and json_equal(e.get("metric"), metric)]
    if select:
        entries = [(i, e) for i, e in entries
                   if all(k != "source" and k in e and json_equal(public_fact_value(e[k]), v)
                          for k, v in select.items())]
    return entries


def fact_present(view: Mapping[str, Any], fact: str) -> bool:
    """Whether a shorthand `fact` path exists in the view (a `performance.<metric>` fact exists when
    an entry has that metric). Used to warn about shorthand paths absent from every view."""
    segments = fact.split(".")
    if segments[0] == "performance" and len(segments) >= 2:
        entries = _metric_entries(view, ["performance"], segments[1])
        if len(segments) == 2:
            return bool(entries)
        return any(_has_path(entry, segments[2:]) for _, entry in entries)
    return _has_path(view, segments)


def _has_path(data: object, segments: Sequence[str]) -> bool:
    for key in segments:
        if not isinstance(data, dict) or key not in data:
            return False
        data = data[key]
    return True


def _interval_pair(obj: Mapping[str, Any]) -> tuple[str, str] | None:
    """The single `min_<x>`/`max_<x>` member pair of an interval object, if exactly one."""
    pairs = [(k, "max_" + k[4:]) for k in obj if k.startswith("min_") and ("max_" + k[4:]) in obj]
    return pairs[0] if len(pairs) == 1 else None


def _default_member(entries: Sequence[object], fact: str, pointer: str | None) -> str:
    """Default predicate path for an object fact (amendment 10.3): `value` if present, else the first
    member matching `^value(_[a-z_]+)?$`. An interval (min_/max_ pair) without a value member needs
    an explicit path; anything else defaults to `value` (and evaluates as VALUE_MISSING)."""
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if "value" in entry:
            return "value"
        for key in entry:
            if _VALUE_MEMBER.match(key):
                return key
    for entry in entries:
        if isinstance(entry, dict):
            pair = _interval_pair(entry)
            if pair is not None:
                raise RequestError(
                    "INVALID_REQUEST",
                    f"{fact} is an interval ({pair[0]}/{pair[1]}); a predicate needs one bound",
                    pointer=pointer,
                    hint=f"give path {pair[0]} or {pair[1]}, or use op covers_interval",
                    details={"fact": fact, "paths": list(pair)},
                )
    return "value"


# ---------------------------------------------------------------------------------------------
# Validation


def _ptr(base: str, *parts: object) -> str:
    return base + "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def _invalid(message: str, pointer: str, hint: str | None = None, **details: Any) -> RequestError:
    return RequestError("INVALID_REQUEST", message, pointer=pointer, hint=hint, details=details)


def _check_keys(raw: Mapping[str, Any], allowed: frozenset[str], pointer: str, what: str) -> None:
    for key in sorted(raw, key=str):
        if key not in allowed:
            close = difflib.get_close_matches(str(key), sorted(allowed), n=3, cutoff=0.6)
            hint = f"did you mean {', '.join(close)}?" if close else f"allowed keys: {', '.join(sorted(allowed))}"
            raise _invalid(f"unknown key {_short(str(key))!r} in {what}", _ptr(pointer, key), hint,
                           did_you_mean=close, allowed=sorted(allowed))


def _check_op(op: object, pointer: str) -> str:
    if not isinstance(op, str) or op not in OPERATORS:
        close = difflib.get_close_matches(str(op), OPERATORS, n=3, cutoff=0.5)
        raise RequestError(
            "UNSUPPORTED_OPERATOR", f"unsupported operator {_short(op)!r}", pointer=pointer,
            hint=f"did you mean {', '.join(close)}?" if close else "see `os-optics-lens help request`",
            details={"op": _short(op), "supported": list(OPERATORS), "did_you_mean": close},
        )
    return op


def _check_predicate_value(op: str, holder: Mapping[str, Any], pointer: str) -> None:
    """Validate `value` (and `tolerance`) of one predicate for its operator."""
    vptr = _ptr(pointer, "value")
    if "tolerance" in holder:
        if op != "includes_all":
            raise _invalid("tolerance applies only to includes_all", _ptr(pointer, "tolerance"))
        tol = holder["tolerance"]
        if not _is_number(tol) or tol < 0:
            raise _invalid("tolerance must be a finite number >= 0", _ptr(pointer, "tolerance"))
    if op in _SOURCE_OPS:
        if "value" in holder and holder["value"] is not True:
            raise _invalid(f"{op} takes value true", vptr)
        return
    if "value" not in holder:
        raise _invalid(f"operator {op} needs a value", vptr)
    value = holder["value"]
    if op in _ORDER_OPS:
        if not _is_number(value):
            raise _invalid(f"operator {op} needs a finite number", vptr, actual=value)
    elif op in _PAIR_OPS:
        if (not isinstance(value, list) or len(value) != 2 or not all(_is_number(v) for v in value)
                or value[0] > value[1]):
            raise _invalid(f"operator {op} needs [low, high] with low <= high", vptr, actual=value)
    elif op == "in":
        if not isinstance(value, list) or not value:
            raise _invalid("operator in needs a non-empty list of choices", vptr)
    elif op == "proportional_weights":
        if (not isinstance(value, list) or not value
                or not all(_is_number(v) and v >= 0 for v in value) or not max(value) > 0):
            raise _invalid("proportional_weights needs a non-empty list of non-negative numbers "
                           "with a positive maximum", vptr)
    elif op == "includes_all":
        if not isinstance(value, list) or not value:
            raise _invalid("includes_all needs a non-empty list", vptr)
        if "tolerance" in holder and not all(_is_number(v) for v in value):
            raise _invalid("includes_all with tolerance needs numbers", vptr)


def _check_common(raw: Mapping[str, Any], pointer: str, *, metric_like: bool) -> None:
    if "id" in raw and (not isinstance(raw["id"], str) or not raw["id"]):
        raise _invalid("id must be a non-empty string", _ptr(pointer, "id"))
    if "conditions" in raw:
        if not isinstance(raw["conditions"], dict):
            raise _invalid("conditions must be an object of key: expected value", _ptr(pointer, "conditions"))
        spectrum = raw["conditions"].get("spectrum")
        if isinstance(spectrum, dict) and "incident_weights" in spectrum \
                and "sampling_wavelengths_nm" not in spectrum:
            raise _invalid("conditions.spectrum gives incident_weights without sampling_wavelengths_nm",
                           _ptr(pointer, "conditions", "spectrum"),
                           "weights are meaningless without their ordered wavelengths; add "
                           "sampling_wavelengths_nm in the same order")
    if "select" in raw:
        select = raw["select"]
        if not isinstance(select, dict) or not select:
            raise _invalid("select must be a non-empty object of member: value", _ptr(pointer, "select"))
        if not metric_like:
            raise _invalid("select narrows performance metric entries", _ptr(pointer, "select"),
                           "use select with metric (fixture form) or a performance.<metric> fact")
    for flag in ("requires_blocking_stop", "requires_evidence_source"):
        if flag in raw and not isinstance(raw[flag], bool):
            raise _invalid(f"{flag} must be true or false", _ptr(pointer, flag))
    if "required_sampled_field_fractions" in raw:
        fractions = raw["required_sampled_field_fractions"]
        if not isinstance(fractions, list) or not fractions or not all(_is_number(v) for v in fractions):
            raise _invalid("required_sampled_field_fractions must be a non-empty list of finite numbers",
                           _ptr(pointer, "required_sampled_field_fractions"),
                           "give the ordered field fractions, or omit the member")


def _check_fact(fact: object, pointer: str) -> str:
    if not isinstance(fact, str) or not _FACT_SYNTAX.match(fact):
        raise _invalid("fact must be a dotted fact key such as first_order.effective_focal_length_mm",
                       pointer, actual=fact)
    return fact


def validate_constraint(raw: object, pointer: str) -> None:
    """Validate one constraint (fixture or shorthand form) against the closed schema.

    Raises `RequestError` (`INVALID_REQUEST` for unknown keys, with `did_you_mean`, and bad shapes;
    `UNSUPPORTED_OPERATOR` for unknown operators). `pointer` locates the constraint in the request.
    """
    if not isinstance(raw, dict):
        raise _invalid("a constraint must be an object", pointer)
    if "fact" in raw and "scope" in raw:
        raise _invalid("give either fact (shorthand) or scope + predicates (fixture form)", pointer)
    if "fact" in raw:
        _check_keys(raw, _SHORTHAND_KEYS, pointer, "constraint")
        fact = _check_fact(raw["fact"], _ptr(pointer, "fact"))
        if "op" not in raw:
            raise _invalid("a shorthand constraint needs op", _ptr(pointer, "op"))
        op = _check_op(raw["op"], _ptr(pointer, "op"))
        if "path" in raw and not isinstance(raw["path"], str):
            raise _invalid("path must be a dotted string", _ptr(pointer, "path"))
        _check_predicate_value(op, raw, pointer)
        segments = fact.split(".")
        _check_common(raw, pointer, metric_like=segments[0] == "performance" and len(segments) >= 2)
        return
    if "scope" not in raw:
        raise _invalid("a constraint needs fact (shorthand) or scope + predicates", pointer,
                       "e.g. {\"fact\": \"first_order.effective_focal_length_mm\", \"op\": \"lte\", \"value\": 30}")
    _check_keys(raw, _FIXTURE_KEYS, pointer, "constraint")
    scope = raw["scope"]
    if not isinstance(scope, list) or not scope or not all(isinstance(s, str) and s for s in scope):
        raise _invalid("scope must be a non-empty list of member names", _ptr(pointer, "scope"))
    if "metric" in raw and (not isinstance(raw["metric"], str) or not raw["metric"]):
        raise _invalid("metric must be a non-empty string", _ptr(pointer, "metric"))
    predicates = raw.get("predicates")
    if not isinstance(predicates, list) or not predicates:
        raise _invalid("predicates must be a non-empty list", _ptr(pointer, "predicates"))
    for index, predicate in enumerate(predicates):
        pptr = _ptr(pointer, "predicates", index)
        if not isinstance(predicate, dict):
            raise _invalid("a predicate must be an object {path, op, value}", pptr)
        _check_keys(predicate, _PREDICATE_KEYS, pptr, "predicate")
        if not isinstance(predicate.get("path"), str):
            raise _invalid("predicate path must be a dotted string", _ptr(pptr, "path"))
        if "op" not in predicate:
            raise _invalid("a predicate needs op", _ptr(pptr, "op"))
        op = _check_op(predicate["op"], _ptr(pptr, "op"))
        _check_predicate_value(op, predicate, pptr)
    _check_common(raw, pointer, metric_like="metric" in raw)


def validate_target(raw: object, pointer: str) -> None:
    """Validate one starting-point target `{id?, fact, target, op?: "near", tolerance?,
    relative_tolerance?, conditions?, weight?, select?, path?, required_sampled_field_fractions?}`."""
    if not isinstance(raw, dict):
        raise _invalid("a target must be an object", pointer)
    _check_keys(raw, _TARGET_KEYS, pointer, "target")
    if "fact" not in raw:
        raise _invalid("a target needs fact", _ptr(pointer, "fact"))
    fact = _check_fact(raw["fact"], _ptr(pointer, "fact"))
    if raw.get("op", "near") != "near":
        raise RequestError("UNSUPPORTED_OPERATOR", "the only target op is near",
                           pointer=_ptr(pointer, "op"), details={"supported": ["near"]})
    if not _is_number(raw.get("target")):
        raise _invalid("target must be a finite number", _ptr(pointer, "target"))
    for key in ("tolerance", "relative_tolerance"):
        if key in raw and (not _is_number(raw[key]) or raw[key] <= 0):
            raise _invalid(f"{key} must be a finite number > 0", _ptr(pointer, key))
    if "weight" in raw and (not _is_number(raw["weight"]) or raw["weight"] < 0):
        raise _invalid("weight must be a finite number >= 0", _ptr(pointer, "weight"))
    scale, basis = target_scale(raw)
    if not (math.isfinite(scale) and scale >= sys.float_info.min):
        key = {"tolerance": "tolerance", "relative_tolerance": "relative_tolerance"}.get(basis, "target")
        raise _invalid(f"the target scale ({basis}) {scale!r} is not a normal positive number",
                       _ptr(pointer, key),
                       "give a larger tolerance (the scale must be at least 2.2250738585072014e-308)",
                       scale_basis=basis, minimum=sys.float_info.min)
    if "path" in raw and not isinstance(raw["path"], str):
        raise _invalid("path must be a dotted string", _ptr(pointer, "path"))
    segments = fact.split(".")
    _check_common(raw, pointer, metric_like=segments[0] == "performance" and len(segments) >= 2)


def validate_preference(raw: object, pointer: str) -> None:
    """Validate one preference: `{fact, direction, conditions?, select?, path?,
    required_sampled_field_fractions?}` or the fixture `ranking_preference` form (recognized by
    scope + metric)."""
    if not isinstance(raw, dict):
        raise _invalid("a preference must be an object", pointer)
    if raw.get("direction") not in _DIRECTIONS:
        raise _invalid("direction must be ascending or descending", _ptr(pointer, "direction"))
    if is_fixture_preference(raw):
        _check_keys(raw, _FIXTURE_PREFERENCE_KEYS, pointer, "preference")
        scope = raw["scope"]
        if not isinstance(scope, list) or not scope or not all(isinstance(s, str) and s for s in scope):
            raise _invalid("scope must be a non-empty list of member names", _ptr(pointer, "scope"))
        if not isinstance(raw["metric"], str) or not raw["metric"]:
            raise _invalid("metric must be a non-empty string", _ptr(pointer, "metric"))
        return
    _check_keys(raw, _PREFERENCE_KEYS, pointer, "preference")
    if "fact" not in raw:
        raise _invalid("a preference needs fact (or the fixture form scope + metric)", _ptr(pointer, "fact"))
    fact = _check_fact(raw["fact"], _ptr(pointer, "fact"))
    if "path" in raw and not isinstance(raw["path"], str):
        raise _invalid("path must be a dotted string", _ptr(pointer, "path"))
    segments = fact.split(".")
    _check_common(raw, pointer, metric_like=segments[0] == "performance" and len(segments) >= 2)


def validate_preferences(raw: object, pointer: str) -> None:
    """Validate the `preferences` member: always a list (a bare object is rejected with a hint)."""
    if isinstance(raw, dict):
        raise _invalid("preferences must be a list", pointer, "wrap the preference object in a list")
    if not isinstance(raw, list):
        raise _invalid("preferences must be a list", pointer)
    for index, item in enumerate(raw):
        validate_preference(item, _ptr(pointer, index))


# ---------------------------------------------------------------------------------------------
# Expansion


def expand_constraint(raw: Mapping[str, Any], view_for_shape: Mapping[str, Any] | None,
                      index: int, *, pointer: str | None = None) -> dict[str, Any]:
    """Validate and normalize one constraint; `id` defaults to `c<index>`.

    The fixture form passes through (deep-copied). A shorthand is normalized (`value` defaults to
    true for has_source/has_sourced_method) and, when `view_for_shape` is given, expanded against
    that view with `expand_for_view`; otherwise it stays shorthand and evaluation expands it per view.
    """
    pointer = pointer if pointer is not None else f"/constraints/{index}"
    validate_constraint(raw, pointer)
    constraint = copy.deepcopy(dict(raw))
    constraint.setdefault("id", f"c{index}")
    if not is_shorthand(constraint):
        return constraint
    if constraint["op"] in _SOURCE_OPS:
        constraint.setdefault("value", True)
    if view_for_shape is None:
        return constraint
    return expand_for_view(constraint, view_for_shape, pointer=pointer)


def expand_for_view(constraint: Mapping[str, Any], view: Mapping[str, Any], *,
                    pointer: str | None = None) -> dict[str, Any]:
    """Expand a shorthand constraint against the fact's shape in `view` (amendment 10.3).

    * `performance.<metric>[.<member>...]` -> scope ["performance"], metric; path = the trailing
      members, else the explicit `path`, else "" for covers_interval, else the first entry's value
      member (`value` preferred). An interval metric without a path raises INVALID_REQUEST.
    * an object at the path -> scope = the full path, path = explicit, "" for covers_interval, else
      the default value member (interval objects raise INVALID_REQUEST).
    * has_source / has_sourced_method on an object or metric fact named without a member default
      the path to `source` / `methods` instead of the value member.
    * a scalar, string, bool, list or null at the path -> scope = the parent, path = the last member.
    * an absent path -> scope = the full path (evaluates as FACT_MISSING).

    A fixture-form constraint is returned unchanged.
    """
    if not is_shorthand(constraint):
        return dict(constraint)
    fact: str = constraint["fact"]
    segments = fact.split(".")
    op = constraint["op"]
    explicit = constraint.get("path")
    out = {k: v for k, v in constraint.items() if k not in _SHORTHAND_PREDICATE_KEYS}
    predicate: dict[str, Any] = {"op": op, "value": constraint.get("value", True)}
    if "tolerance" in constraint:
        predicate["tolerance"] = constraint["tolerance"]
    if segments[0] == "performance" and len(segments) >= 2:
        out["scope"] = ["performance"]
        out["metric"] = segments[1]
        rest = segments[2:]
        if rest:
            path = ".".join(rest + ([explicit] if explicit else []))
        elif explicit is not None:
            path = explicit
        elif op == "covers_interval":
            path = ""
        elif op in _SOURCE_DEFAULT_PATH:
            path = _SOURCE_DEFAULT_PATH[op]
        else:
            entries = [e for _, e in _metric_entries(view, ["performance"], segments[1],
                                                     constraint.get("select"))]
            path = _default_member(entries, fact, pointer)
    else:
        parent = _at(view, segments[:-1])
        leaf = segments[-1]
        if op == "has_source" and leaf == "source" and explicit is None:
            out["scope"] = segments[:-1]
            path = leaf
        elif isinstance(parent, dict) and leaf in parent and isinstance(parent[leaf], dict):
            out["scope"] = segments
            if explicit is not None:
                path = explicit
            elif op == "covers_interval":
                path = ""
            elif op in _SOURCE_DEFAULT_PATH:
                path = _SOURCE_DEFAULT_PATH[op]
            else:
                path = _default_member([parent[leaf]], fact, pointer)
        elif isinstance(parent, dict) and leaf in parent:
            out["scope"] = segments[:-1]
            path = leaf + ("." + explicit if explicit else "")
        else:
            out["scope"] = segments
            if explicit is not None:
                path = explicit
            elif op == "covers_interval":
                path = ""
            else:
                path = _SOURCE_DEFAULT_PATH.get(op, "value")
    predicate["path"] = path
    out["predicates"] = [predicate]
    return out


# ---------------------------------------------------------------------------------------------
# Locating the comparable fact (§6.2 steps 1-6)


@dataclass
class _Located:
    """Outcome of steps 1-6: either a failure reason or the single comparable fact."""

    reason: str | None = None
    details: dict[str, Any] | None = None
    fact: Any = None
    json_path: str = ""
    key_segments: list[str] = field(default_factory=list)
    gate_required: bool = False
    stop: dict[str, Any] = field(default_factory=dict)


def _alternatives(view: Mapping[str, Any], scope: Sequence[str], metric: str | None,
                  ctx: EvalContext) -> list[dict[str, Any]]:
    """Sibling facts of the same quantity family, listed for FACT_MISSING (never substituted)."""
    if metric is not None:
        container = _at(view, scope)
        if not isinstance(container, list):
            return []
        counts: dict[str, int] = {}
        for entry in container:
            if isinstance(entry, dict) and isinstance(entry.get("metric"), str):
                counts[entry["metric"]] = counts.get(entry["metric"], 0) + 1
        family = metric.split("_")[0]
        names = [m for m in counts if m != metric and m.split("_")[0] == family] or \
                [m for m in counts if m != metric]
        prefix = ".".join(scope)
        return [{"fact": f"{prefix}.{m}", "entries": counts[m]} for m in sorted(names)[:10]]
    if len(scope) < 2:
        return []
    parent = _at(view, scope[:-1])
    if not isinstance(parent, dict):
        return []
    member = scope[-1]
    section_key = ".".join(scope[:-1])
    # Every aperture quantity (f-numbers, pupil diameters, NA) is an alternative convention of the
    # same system property; elsewhere the family is the unit class of the member name.
    whole_section = section_key == "aperture" or section_key in ctx.stop_dependent_sections
    wanted_unit = unit_for_name(member)
    out = []
    for name in sorted(parent):
        if name == member or name in _NOT_FACTS:
            continue
        item = parent[name]
        if isinstance(item, dict) and _is_number(item.get("value")):
            value = item["value"]
        elif _is_number(item):
            value = item
        else:
            continue
        if not whole_section and unit_for_name(name) != wanted_unit:
            continue
        alt: dict[str, Any] = {"fact": f"{section_key}.{name}", "value": value}
        if isinstance(item, dict) and "system_aperture" in item:
            alt["system_aperture"] = item["system_aperture"]
        out.append(alt)
    return out[:10]


def _nearest_condition_details(candidates: Sequence[tuple[str, Any]],
                               conditions: Mapping[str, Any]) -> dict[str, Any]:
    """Per-key expected vs actual for the candidate agreeing on the most condition keys."""
    best: tuple[int, int] | None = None
    best_details: dict[str, Any] = {}
    for position, (path, fact) in enumerate(candidates):
        own = fact.get("conditions") if isinstance(fact, dict) else None
        differs: dict[str, Any] = {}
        agreed = 0
        for key, expected in conditions.items():
            found = _condition_differences(own, key, expected)
            if found:
                differs.update(found)
            else:
                agreed += 1
        rank = (agreed, -position)
        if best is None or rank > best:
            best = rank
            best_details = {"json_path": path, "candidates": len(candidates), "differs": differs}
            if isinstance(own, str):
                best_details["conditions_text"] = _short(own)
            elif own is None:
                best_details["note"] = "the fact has no structured conditions"
    return best_details


def _ambiguity_details(candidates: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    """Members whose values distinguish the comparable candidates."""
    keys: set[str] = set()
    for _, fact in candidates:
        if isinstance(fact, dict):
            keys.update(fact)
    distinguishing: dict[str, list[Any]] = {}
    for key in sorted(keys):
        if key in ("source", "condition", "definition", "basis", "metric") or _VALUE_MEMBER.match(key):
            continue
        seen: dict[str, Any] = {}
        for _, fact in candidates:
            value = public_fact_value(fact.get(key)) if isinstance(fact, dict) else None
            seen.setdefault(_canonical_text(value), value)
        if len(seen) > 1:
            distinguishing[key] = list(seen.values())[:10]
        if len(distinguishing) >= 6:
            break
    return {
        "candidates": len(candidates),
        "json_paths": [path for path, _ in candidates][:10],
        "distinguishing": distinguishing,
        "hint": "pin one fact with conditions or select",
    }


def _discovery_hit(path: str, obj: object) -> dict[str, Any] | None:
    if isinstance(obj, dict) and obj.get("eligible_for") == _DISCOVERY_MARK:
        details: dict[str, Any] = {
            "json_path": path, "eligible_for": _DISCOVERY_MARK,
            "note": "discovery-only values never satisfy a hard constraint",
        }
        if path.endswith("sampling_span_nm"):
            details["hint"] = ("use spectrum.continuous_coverage_nm with covers_interval for "
                               "coverage, or spectrum.sampling_wavelengths_nm for samples")
        return details
    return None


def _sampling_grid(fact: object) -> object:
    """The sampling grid a resolved fact carries (`sampled_field_fractions`), or None."""
    return fact.get(_SAMPLING_MEMBER) if isinstance(fact, dict) else None


def _sampling_differs(fact: object, required: object) -> dict[str, Any] | None:
    """§6.2 step 5: SAMPLING_DIFFERS details when the fact's grid is not the required one."""
    if not isinstance(fact, dict) or _SAMPLING_MEMBER not in fact:
        return {"expected": required, "present": False}
    if json_equal(fact[_SAMPLING_MEMBER], required):
        return None
    return {"expected": required, "actual": fact[_SAMPLING_MEMBER]}


def _ranking_fact(fact: object, path: str) -> object:
    """Find the nearest object declaring the grid for the numeric member at path."""
    owner = node = fact
    for segment in _split(path):
        node = node.get(segment) if isinstance(node, dict) else None
        if _sampling_grid(node) is not None:
            owner = node
    return owner


def _locate(view: Mapping[str, Any], c: Mapping[str, Any], ctx: EvalContext, *,
            sampling_and_evidence: bool, ranking_path: str | None = None) -> _Located:
    """Apply stop eligibility, scope, comparability, discovery-only, sampling and evidence.

    Ranking uses a requested grid to narrow entries before checking for ambiguity. The caller
    applies the stop satisfaction policy and validates the selected value.
    """
    scope: list[str] = list(c["scope"])
    metric = c.get("metric")
    key_segments = scope + ([metric] if metric is not None else [])
    stop = stop_status(view)
    required = bool(c.get("requires_blocking_stop")) or \
        _prefix_stop_dependent(key_segments, ctx) is not None
    located = _Located(key_segments=key_segments, gate_required=required, stop=stop)
    # Selectors cannot use evidence references or intake checks to choose the compared fact.
    select_keys = list(c.get("select") or {})
    condition_keys = list(c.get("conditions") or {})
    not_fact = (_not_fact_hit(key_segments) or _source_path_hit(key_segments)
                or _not_fact_hit(select_keys, "select") or _not_fact_hit(condition_keys, "conditions"))
    if not_fact is None and "source" in select_keys:
        not_fact = _source_path_hit(["source"], "select")
    if not_fact is None and "source" in condition_keys:
        not_fact = _source_path_hit(["source"], "conditions")
    predicates = c.get("predicates") or ()
    if not_fact is None and len(predicates) == 1:
        not_fact = _not_fact_hit(_split(predicates[0].get("path", "")), ".".join(key_segments))
    if not_fact is not None:
        located.reason, located.details = COMPARISON_ONLY, not_fact
        return located
    if required and not stop["eligible"]:
        located.reason = STOP_NOT_ELIGIBLE
        located.details = {
            "state": stop["state"], "fact": ".".join(key_segments),
            "note": "stop-dependent evaluation needs a stop that blocks light and an eligible record",
        }
        return located

    base = ".".join(scope)
    if metric is not None:
        entries = _metric_entries(view, scope, metric)
        if not entries:
            located.reason = FACT_MISSING
            located.details = {"fact": ".".join(key_segments),
                               "available_alternatives": _alternatives(view, scope, metric, ctx)}
            return located
        select = c.get("select")
        if select:
            narrowed = _metric_entries(view, scope, metric, select)
            if not narrowed:
                available = {}
                for key in select:
                    seen: dict[str, Any] = {}
                    for _, entry in entries:
                        if key in entry:
                            member = public_fact_value(entry[key])
                            seen.setdefault(_canonical_text(member), member)
                    available[key] = {"expected": select[key], "available": list(seen.values())[:10]}
                    if not seen:
                        available[key]["present"] = False
                located.reason = CONDITIONS_DIFFER
                located.details = {"select": available, "candidates": len(entries)}
                return located
            entries = narrowed
        candidates = [(f"{base}[{i}]", entry) for i, entry in entries]
    else:
        obj = _at(view, scope)
        if obj is None:
            located.reason = FACT_MISSING
            located.details = {"fact": base,
                               "available_alternatives": _alternatives(view, scope, None, ctx)}
            return located
        candidates = [(base, obj)]

    conditions = c.get("conditions") or {}
    if conditions:
        comparable = [
            (path, fact) for path, fact in candidates
            if isinstance(fact, dict) and _conditions_match(fact.get("conditions"), conditions)
        ]
    else:
        comparable = list(candidates)
    if not comparable:
        located.reason = CONDITIONS_DIFFER
        located.details = _nearest_condition_details(candidates, conditions)
        return located
    if len(comparable) > 1 and ranking_path is not None and "required_sampled_field_fractions" in c:
        required_grid = c["required_sampled_field_fractions"]
        grid_matches = [
            (path, fact) for path, fact in comparable
            if _sampling_differs(_ranking_fact(fact, ranking_path), required_grid) is None
        ]
        if not grid_matches:
            located.reason = SAMPLING_DIFFERS
            located.details = _sampling_differs(_ranking_fact(comparable[0][1], ranking_path), required_grid)
            return located
        comparable = grid_matches
    if len(comparable) > 1:
        located.reason = AMBIGUOUS_FACTS
        located.details = _ambiguity_details(comparable)
        return located
    located.json_path, located.fact = comparable[0]

    node: object = view
    walked: list[str] = []
    for segment in scope:
        node = node.get(segment) if isinstance(node, dict) else None
        walked.append(segment)
        hit = _discovery_hit(".".join(walked), node)
        if hit:
            located.reason, located.details = DISCOVERY_ONLY, hit
            return located
    hit = _discovery_hit(located.json_path, located.fact)
    if hit:
        located.reason, located.details = DISCOVERY_ONLY, hit
        return located

    fact = located.fact
    if "required_sampled_field_fractions" in c:
        owner = _ranking_fact(fact, ranking_path) if ranking_path is not None else fact
        differs = _sampling_differs(owner, c["required_sampled_field_fractions"])
        if differs is not None:
            located.reason, located.details = SAMPLING_DIFFERS, differs
            return located
    if sampling_and_evidence and c.get("requires_evidence_source"):
        source = fact.get("source") if isinstance(fact, dict) else None
        status = ctx.source_status(source)
        if status != RESOLVED:
            located.reason = EVIDENCE_MISSING if status == MISSING else EVIDENCE_UNRESOLVED
            located.details = {"status": status}
            if isinstance(source, dict):
                located.details["source"] = {"path": source.get("path"), "section": source.get("section")}
            return located
    return located


# ---------------------------------------------------------------------------------------------
# Predicates (§6.2 steps 7-8, §6.3)


@dataclass
class _PredicateResult:
    outcome: str
    path: str
    op: str
    reason: str | None = None
    details: dict[str, Any] | None = None
    shortfall: dict[str, Any] | None = None
    value: Any = None


def _coverage_guard(view: Mapping[str, Any], full: Sequence[str]) -> dict[str, Any] | None:
    """10.2 (iii): a path through continuous coverage whose state is not confirmed (or whose
    bounds are missing). A predicate on `state` itself is exempt."""
    if tuple(full[:2]) != _COVERAGE_KEY or list(full[2:]) == ["state"]:
        return None
    coverage = _at(view, list(_COVERAGE_KEY))
    state = coverage.get("state") if isinstance(coverage, dict) else None
    confirmed = (isinstance(coverage, dict) and isinstance(state, str) and state in _CONFIRMED_COVERAGE_STATES
                 and _is_number(coverage.get("min_nm")) and _is_number(coverage.get("max_nm")))
    if confirmed:
        return None
    span = _at(view, ["spectrum", "sampling_span_nm"])
    bounds = [span.get("min_nm"), span.get("max_nm")] if isinstance(span, dict) else None
    return {"state": state, "sampling_span_nm": bounds, "note": "sampling span is discovery-only"}


def _path_guards(view: Mapping[str, Any], located: _Located, segments: Sequence[str],
                 ctx: EvalContext, *, op: str | None = None) -> tuple[str | None, dict[str, Any] | None, bool]:
    """Amendment 10.2 guards for one predicate path: returns (reason, details, stop_gated).

    Besides the path itself, the value the path ends at is checked as a whole: a predicate that
    compares an object or list containing stop-dependent facts, comparison-only checks,
    discovery-only objects or unconfirmed coverage is guarded exactly like a predicate on that
    contained member."""
    full = located.key_segments + list(segments)
    not_fact = _not_fact_hit(segments, located.json_path)
    if not (op == "has_source" and segments and segments[-1] == "source" and "source" not in segments[:-1]):
        not_fact = not_fact or _source_path_hit(segments, located.json_path)
    if not_fact is not None:
        return COMPARISON_ONLY, not_fact, False
    target: object = located.fact
    for segment in segments:
        target = target.get(segment) if isinstance(target, dict) else None
    gated = False
    if not located.gate_required:
        key = _prefix_stop_dependent(full, ctx) or _contained_stop_dependent(full, target, ctx)
        if key is not None:
            gated = True
            if not located.stop["eligible"]:
                return STOP_NOT_ELIGIBLE, {"state": located.stop["state"], "fact": key}, gated
    node: object = located.fact
    walked = located.json_path
    for segment in segments:
        node = node.get(segment) if isinstance(node, dict) else None
        walked = f"{walked}.{segment}" if walked else segment
        hit = _discovery_hit(walked, node)
        if hit:
            return DISCOVERY_ONLY, hit, gated
    contained = _contained_hit(target, walked)
    if contained is not None:
        return contained[0], contained[1], gated
    coverage = _coverage_guard(view, full)
    if coverage is None and len(full) < len(_COVERAGE_KEY) \
            and tuple(full) == _COVERAGE_KEY[: len(full)] and _has_path(target, _COVERAGE_KEY[len(full):]):
        coverage = _coverage_guard(view, _COVERAGE_KEY)
    if coverage is not None:
        return COVERAGE_UNCONFIRMED, coverage, gated
    return None, None, gated


def _interval_bounds(value: object) -> tuple[object, object] | None:
    """The (low, high) bounds of an interval *object* (one min_/max_ member pair), else None.

    A bare list is never an interval: two sampled wavelengths `[532, 1064]` say nothing about the
    gap between them, so covers_interval on a list is VALUE_TYPE (compare samples with includes_all).
    """
    if isinstance(value, dict):
        pair = _interval_pair(value)
        if pair is not None:
            return value.get(pair[0]), value.get(pair[1])
    return None


def _value_unit(located: _Located, segments: Sequence[str], value: object) -> str | None:
    last = segments[-1] if segments else None
    if last is not None and last != "value":
        unit = unit_for_name(last)
        if unit is not None:
            return unit
    metric_fact = len(located.key_segments) > 0 and located.json_path.endswith("]")
    if metric_fact:
        return "1" if _is_number(value) else None
    names = [s for s in located.key_segments if s != "value"]
    return unit_for_name(names[-1]) if names else None


def _numeric_shortfall(cid: str, json_path: str, value: float, limit: float, unit: str | None,
                       strict: bool = False) -> dict[str, Any]:
    gap = _finite_or_none(lambda: abs(value - limit))
    out: dict[str, Any] = {
        "constraint": cid, "path": json_path, "value": value, "limit": limit, "shortfall": gap,
        "relative": _finite_or_none(lambda: gap / abs(limit)) if gap is not None and limit != 0 else None,
    }
    if unit is not None:
        out["unit"] = unit
    if strict:
        out["strict"] = True
    return out


def _evaluate_predicate(view: Mapping[str, Any], located: _Located, predicate: Mapping[str, Any],
                        cid: str, ctx: EvalContext) -> _PredicateResult:
    path = predicate.get("path", "")
    op = predicate["op"]
    expected = predicate.get("value", True)
    segments = _split(path)
    result = _PredicateResult(UNCONFIRMED, path, op)
    reason, details, gated = _path_guards(view, located, segments, ctx, op=op)
    if reason is not None:
        result.reason, result.details = reason, details
        return result
    value = _at(located.fact, segments) if segments else located.fact
    if op not in _SOURCE_OPS:
        value = public_fact_value(value)
    result.value = public_fact_value(value) if op == "has_sourced_method" else value
    json_path = f"{located.json_path}.{path}" if path else located.json_path
    if value is None:
        result.reason = VALUE_MISSING
        result.details = {"path": path or None}
        if isinstance(located.fact, dict) and segments == ["value"]:
            result.details["numeric_members"] = sorted(
                k for k, v in located.fact.items() if _is_number(v))
        return result

    outcome: str
    if op in ("eq", "ne"):
        equal = json_equal(value, expected)
        outcome = MATCH if equal == (op == "eq") else DOES_NOT_MATCH
    elif op == "in":
        outcome = MATCH if any(json_equal(value, choice) for choice in expected) else DOES_NOT_MATCH
    elif op in _ORDER_OPS or op == "between_inclusive":
        if not _is_number(value):
            result.reason, result.details = VALUE_TYPE, {"path": path, "expected": "number",
                                                         "actual": _short(value)}
            return result
        unit = _value_unit(located, segments, value)
        if op == "between_inclusive":
            low, high = expected
            if value < low:
                result.shortfall = _numeric_shortfall(cid, json_path, value, low, unit)
            elif value > high:
                result.shortfall = _numeric_shortfall(cid, json_path, value, high, unit)
            outcome = DOES_NOT_MATCH if result.shortfall else MATCH
        else:
            holds = {"gt": value > expected, "gte": value >= expected,
                     "lt": value < expected, "lte": value <= expected}[op]
            outcome = MATCH if holds else DOES_NOT_MATCH
            if not holds:
                strict = op in ("gt", "lt") and value == expected
                result.shortfall = _numeric_shortfall(cid, json_path, value, expected, unit, strict)
    elif op == "proportional_weights":
        if not isinstance(value, list):
            result.reason, result.details = VALUE_TYPE, {"path": path, "expected": "list"}
            return result
        outcome = MATCH if proportional_weights(value, expected) else DOES_NOT_MATCH
    elif op == "has_source":
        status = ctx.source_status(value)
        if status == RESOLVED:
            outcome = MATCH
        else:
            result.reason = EVIDENCE_MISSING if status == MISSING else EVIDENCE_UNRESOLVED
            result.details = {"path": path, "status": status}
            return result
    elif op == "has_sourced_method":
        if not isinstance(value, list):
            result.reason, result.details = VALUE_TYPE, {"path": path, "expected": "list"}
            return result
        statuses = [ctx.source_status(m.get("source")) for m in value
                    if isinstance(m, dict) and m.get("source") is not None]
        if RESOLVED in statuses:
            outcome = MATCH
        else:
            result.reason = EVIDENCE_UNRESOLVED if statuses else EVIDENCE_MISSING
            result.details = {"path": path, "methods": len(value), "sourced": len(statuses),
                              "statuses": sorted(set(statuses))}
            return result
    elif op == "covers_interval":
        bounds = _interval_bounds(value)
        if bounds is None:
            result.reason, result.details = VALUE_TYPE, {
                "path": path, "expected": "interval object (one min_/max_ member pair)",
                "actual": _short(value),
                "hint": "covers_interval needs established continuous coverage "
                        "(spectrum.continuous_coverage_nm); compare sampled values with includes_all"}
            return result
        low, high = bounds
        if low is None or high is None:
            result.reason, result.details = VALUE_MISSING, {"path": path, "bounds": [low, high]}
            return result
        if not (_is_number(low) and _is_number(high)):
            result.reason, result.details = VALUE_TYPE, {"path": path, "expected": "numeric bounds"}
            return result
        want_low, want_high = expected
        if low <= want_low and high >= want_high:
            outcome = MATCH
        else:
            outcome = DOES_NOT_MATCH
            gap = _finite_or_none(lambda: max(0, low - want_low) + max(0, want_high - high))
            width = _finite_or_none(lambda: want_high - want_low)
            result.shortfall = {
                "constraint": cid, "path": json_path, "value": [low, high],
                "limit": [want_low, want_high], "shortfall": gap,
                "relative": _finite_or_none(lambda: gap / width) if gap is not None and width else None,
            }
            unit = _value_unit(located, segments, low)
            if unit is not None:
                result.shortfall["unit"] = unit
    elif op == "includes_all":
        if not isinstance(value, list):
            result.reason, result.details = VALUE_TYPE, {"path": path, "expected": "list"}
            return result
        tolerance = predicate.get("tolerance")
        if tolerance is None:
            missing = [w for w in expected if not any(json_equal(w, v) for v in value)]
        else:
            missing = [w for w in expected
                       if not any(_is_number(v) and abs(v - w) <= tolerance for v in value)]
        outcome = DOES_NOT_MATCH if missing else MATCH
        if missing:
            result.details = {"path": path, "missing": missing}
    else:  # pragma: no cover - validation rejects unknown operators
        raise RequestError("UNSUPPORTED_OPERATOR", f"unsupported operator {op!r}")

    if outcome == MATCH and gated and not located.stop["satisfies"]:
        result.reason = STOP_MATCH_NOT_SUPPORTED
        result.details = _stop_details(located.stop)
        return result
    result.outcome = outcome
    return result


# ---------------------------------------------------------------------------------------------
# Constraint and configuration evaluation


def _basis(view: Mapping[str, Any], located: _Located, segments: Sequence[str]) -> object:
    """Own basis, else the sibling `<stem>_basis` of a scalar member, else the nearest enclosing."""
    fact = located.fact
    if isinstance(fact, dict):
        target = _at(fact, segments) if segments else fact
        if isinstance(target, dict) and isinstance(target.get("basis"), str):
            return target["basis"]
        if len(segments) == 1:
            sibling = fact.get(_unit_stem(segments[0]) + "_basis")
            if isinstance(sibling, str):
                return sibling
        if isinstance(fact.get("basis"), str):
            return fact["basis"]
    scope = list(located.key_segments)
    if located.json_path.endswith("]"):
        scope = scope[:-1]
    for end in range(len(scope) - 1, -1, -1):
        node = _at(view, scope[:end])
        if isinstance(node, dict) and isinstance(node.get("basis"), str):
            return node["basis"]
    return None


def _fact_info(view: Mapping[str, Any], located: _Located, results: Sequence[_PredicateResult],
               ctx: EvalContext, *, include_source: bool) -> dict[str, Any]:
    fact = located.fact
    first = _split(results[0].path) if results else []
    info: dict[str, Any] = {"json_path": located.json_path}
    if len(results) == 1:
        info["value"] = results[0].value
        info["unit"] = _value_unit(located, first, results[0].value)
    else:
        info["values"] = {r.path: r.value for r in results}
    info["basis"] = _basis(view, located, first if len(results) == 1 else [])
    if isinstance(fact, dict):
        if _sampling_grid(fact) is not None:
            info[_SAMPLING_MEMBER] = fact[_SAMPLING_MEMBER]
        if isinstance(fact.get("conditions"), dict):
            info["conditions"] = public_fact_value(fact["conditions"])
        elif isinstance(fact.get("conditions"), str):
            info["conditions_text"] = fact["conditions"]
        source = fact.get("source")
        if include_source and source is not None:
            info["source"] = {
                "path": source.get("path") if isinstance(source, dict) else None,
                "section": source.get("section") if isinstance(source, dict) else None,
                "status": ctx.source_status(source),
            }
    return info


def _unpinned(fact: object, constraint: Mapping[str, Any]) -> dict[str, Any]:
    """Comparison conditions of the matched fact the constraint did not pin (amendment 10.11):
    prose conditions, the sampling grid, condition keys and the members of a partly pinned
    `spectrum`."""
    if not isinstance(fact, dict):
        return {}
    conditions = constraint.get("conditions") or {}
    own = public_fact_value(fact.get("conditions"))
    items: list[tuple[str, Any]] = []
    if isinstance(own, str):
        items.append(("_text", _short(own)))
    # The sampling grid and the unrequested members of a partly pinned spectrum come first. The
    # limit is above the condition count of any measurement recorded so far, so a condition such as
    # the spatial frequency is not folded into `_more`.
    if _sampling_grid(fact) is not None and "required_sampled_field_fractions" not in constraint:
        items.append((_SAMPLING_MEMBER, _short(fact[_SAMPLING_MEMBER])))
    if isinstance(own, dict):
        wanted, stored = conditions.get("spectrum"), own.get("spectrum")
        if isinstance(wanted, dict) and isinstance(stored, dict):
            items.extend((f"spectrum.{member}", _short(stored[member]))
                         for member in sorted(stored) if member not in wanted)
        items.extend((key, _short(own[key])) for key in sorted(own) if key not in conditions)
    if len(items) > _UNPINNED_LIMIT:
        kept = items[: _UNPINNED_LIMIT - 1]
        return {**dict(kept), "_more": len(items) - len(kept)}
    return dict(items)


def evaluate_constraint(view: Mapping[str, Any], constraint: Mapping[str, Any], ctx: EvalContext,
                        *, detail: bool = False) -> dict[str, Any]:
    """Evaluate one constraint against one configuration view (§6.2, amendments 10.1-10.4).

    Returns `{"id", "outcome", "reason"?, "details"?, "shortfalls"?, "unpinned"?, "fact"?,
    "predicates"?}`: `reason` is set exactly when the outcome is UNCONFIRMED; `shortfalls` lists the
    numeric gaps of a DOES_NOT_MATCH; `unpinned` (MATCH only) holds the matched fact's condition
    keys the constraint did not pin, prose conditions as `_text`; `fact` and `predicates` are added
    only with `detail=True`. A shorthand constraint is expanded against this view first.
    """
    c = expand_for_view(constraint, view) if is_shorthand(constraint) else constraint
    cid = str(c.get("id") or "c0")
    located = _locate(view, c, ctx, sampling_and_evidence=True)
    out: dict[str, Any] = {"id": cid}
    if located.reason is not None:
        out["outcome"] = UNCONFIRMED
        out["reason"] = located.reason
        if located.details:
            out["details"] = located.details
        return out

    results = [_evaluate_predicate(view, located, p, cid, ctx) for p in c["predicates"]]
    outcomes = [r.outcome for r in results]
    if DOES_NOT_MATCH in outcomes:
        outcome = DOES_NOT_MATCH
    elif UNCONFIRMED in outcomes:
        outcome = UNCONFIRMED
    else:
        outcome = MATCH
    out["outcome"] = outcome
    if outcome == UNCONFIRMED:
        first = next(r for r in results if r.outcome == UNCONFIRMED)
        out["reason"] = first.reason
        details = dict(first.details or {})
        if len(results) > 1:
            details["predicate"] = results.index(first)
        if details:
            out["details"] = details
    elif outcome == MATCH and located.gate_required and not located.stop["satisfies"]:
        out["outcome"] = UNCONFIRMED
        out["reason"] = STOP_MATCH_NOT_SUPPORTED
        out["details"] = _stop_details(located.stop)
    elif outcome == DOES_NOT_MATCH:
        shortfalls = [r.shortfall for r in results if r.shortfall is not None]
        if shortfalls:
            out["shortfalls"] = shortfalls
        missing = [r.details for r in results if r.outcome == DOES_NOT_MATCH and r.details]
        if missing:
            out["details"] = missing[0] if len(missing) == 1 else {"predicates": missing}
    if out["outcome"] == MATCH:
        unpinned = _unpinned(located.fact, c)
        if unpinned:
            out["unpinned"] = unpinned
    if detail:
        include_source = bool(c.get("requires_evidence_source")) or any(r.op in _SOURCE_OPS for r in results)
        out["fact"] = _fact_info(view, located, results, ctx, include_source=include_source)
        out["predicates"] = [
            {k: v for k, v in (("path", r.path), ("op", r.op), ("outcome", r.outcome),
                               ("reason", r.reason), ("value", r.value)) if v is not None or k == "value"}
            for r in results
        ]
    return out


def evaluate_configuration(view: Mapping[str, Any], constraints: Sequence[Mapping[str, Any]],
                           ctx: EvalContext, detail: bool = False) -> dict[str, Any]:
    """Evaluate every constraint against the same configuration view (§6.4).

    Outcome: any DOES_NOT_MATCH -> DOES_NOT_MATCH; else any UNCONFIRMED -> UNCONFIRMED; else MATCH
    (also for an empty constraint list). Constraints without `id` are named `c<index>`.
    """
    evaluations = []
    for index, constraint in enumerate(constraints):
        if not constraint.get("id"):
            constraint = {**constraint, "id": f"c{index}"}
        evaluations.append(evaluate_constraint(view, constraint, ctx, detail=detail))
    counts = {outcome: 0 for outcome in OUTCOMES}
    for evaluation in evaluations:
        counts[evaluation["outcome"]] += 1
    if counts[DOES_NOT_MATCH]:
        outcome = DOES_NOT_MATCH
    elif counts[UNCONFIRMED]:
        outcome = UNCONFIRMED
    else:
        outcome = MATCH
    return {"outcome": outcome, "counts": counts, "constraints": evaluations}


def configuration_rank(evaluation: Mapping[str, Any], ordinal: int) -> tuple[int, int, int, int]:
    """Sort key choosing a record's best configuration (§6.4): outcome MATCH < UNCONFIRMED < DNM,
    then more MATCH constraints, then fewer DOES_NOT_MATCH, then configuration ordinal."""
    counts = evaluation["counts"]
    return (OUTCOMES.index(evaluation["outcome"]), -counts[MATCH], counts[DOES_NOT_MATCH], ordinal)


# ---------------------------------------------------------------------------------------------
# Targets and preferences (§6.5, amendment 10.9)


def ranking_conditions_equal(actual: object, expected: object) -> bool:
    """Compare full structured ranking conditions, including incident weights by ratio."""
    if json_equal(actual, expected):
        return True
    if not isinstance(actual, dict) or not isinstance(expected, dict) or actual.keys() != expected.keys():
        return False
    actual_spectrum, expected_spectrum = actual.get("spectrum"), expected.get("spectrum")
    if not isinstance(actual_spectrum, dict) or not isinstance(expected_spectrum, dict):
        return False
    return (actual_spectrum.keys() == expected_spectrum.keys()
            and "sampling_wavelengths_nm" in expected_spectrum
            and _conditions_match(actual, expected))


def _single_numeric(view: Mapping[str, Any], spec: Mapping[str, Any], ctx: EvalContext,
                    pointer: str | None) -> tuple[float | None, str | None, dict[str, Any]]:
    """Steps 1-6 with eligibility only, then exactly one comparable finite number."""
    shorthand: dict[str, Any] = {"id": spec.get("id") or "t", "fact": spec["fact"], "op": "near",
                                 "value": spec.get("target")}
    for key in ("path", "conditions", "select", "required_sampled_field_fractions"):
        if key in spec:
            shorthand[key] = spec[key]
    fact_parts = spec["fact"].split(".")
    default_metric_path = fact_parts[0] == "performance" and len(fact_parts) == 2 and "path" not in spec
    if default_metric_path:
        shorthand["path"] = ""
    try:
        c = expand_for_view(shorthand, view, pointer=pointer)
        path = c["predicates"][0]["path"]
        located = _locate(view, c, ctx, sampling_and_evidence=False, ranking_path=path)
        if default_metric_path and located.reason is None:
            path = _default_member([located.fact], spec["fact"], pointer)
    except RequestError as error:
        raise RequestError("INVALID_REQUEST", f"{spec['fact']} is an interval and cannot be ranked "
                           "as one value", pointer=pointer, hint=error.hint,
                           details=error.details) from None
    info: dict[str, Any] = {"fact": spec["fact"]}
    if located.reason is not None:
        if located.details:
            info["details"] = located.details
        return None, located.reason, info
    owner = _ranking_fact(located.fact, path)
    info["sampled_field_fractions"] = _sampling_grid(owner)
    conditions = owner.get("conditions") if isinstance(owner, dict) else None
    if conditions is None and isinstance(located.fact, dict):
        conditions = located.fact.get("conditions")
    if conditions is None and isinstance(owner, dict):
        conditions = owner.get("condition")
    info["conditions"] = public_fact_value(conditions) if isinstance(conditions, (dict, str)) else None
    info["value_member"] = path
    segments = _split(path)
    reason, details, _ = _path_guards(view, located, segments, ctx)
    if reason is not None:
        info["details"] = details
        return None, reason, info
    value = _at(located.fact, segments) if segments else located.fact
    info["json_path"] = f"{located.json_path}.{path}" if path else located.json_path
    info["unit"] = _value_unit(located, segments, value)
    info["basis"] = _basis(view, located, segments)
    if value is None:
        return None, VALUE_MISSING, info
    if isinstance(value, dict) and _interval_pair(value) is not None:
        pair = _interval_pair(value)
        raise RequestError("INVALID_REQUEST", f"{spec['fact']} is an interval and cannot be ranked "
                           "as one value", pointer=pointer,
                           hint=f"give path {pair[0]} or {pair[1]}", details={"fact": spec["fact"]})
    if not _is_number(value):
        info["details"] = {"expected": "number", "actual": _short(public_fact_value(value))}
        return None, VALUE_TYPE, info
    return value, None, info


def target_value(view: Mapping[str, Any], target: Mapping[str, Any], ctx: EvalContext, *,
                 pointer: str | None = None) -> tuple[float | None, str | None, dict[str, Any]]:
    """The value a flexible target compares against (amendment 10.9).

    Resolution follows §6.2 steps 1-6 with stop *eligibility* only (no satisfaction gate) and the
    10.3 shorthand expansion; the target needs exactly one comparable finite number. Returns
    `(value, None, info)` or `(None, reason, info)`. The value's grid and structured conditions in
    `info` let the query check comparability across configurations. Interval facts raise
    `RequestError` INVALID_REQUEST.
    """
    return _single_numeric(view, target, ctx, pointer)


def target_scale(target: Mapping[str, Any]) -> tuple[float, str]:
    """`(scale, scale_basis)` of a target: `tolerance` if given, else `relative_tolerance * |target|`
    if given and target != 0, else `|target|` if != 0, else 1.0 ("absolute")."""
    goal = target["target"]
    if target.get("tolerance") is not None:
        return float(target["tolerance"]), "tolerance"
    if target.get("relative_tolerance") is not None and goal != 0:
        scale = _finite_or_none(lambda: target["relative_tolerance"] * abs(goal))
        return math.inf if scale is None else scale, "relative_tolerance"
    if goal != 0:
        return float(abs(goal)), "target"
    return 1.0, "absolute"


def target_deviation(value: float, target: Mapping[str, Any], *,
                     pointer: str | None = None) -> dict[str, Any]:
    """Deviation of `value` from a target (amendment 10.9).

    `deviation = |value - target|`; `normalized = deviation / scale` with the scale of
    `target_scale` (`scale_basis` "absolute": the absolute deviation). A scale that is not a normal
    positive number, or a normalized deviation that is not finite, raises INVALID_REQUEST (the
    ranking value must be a finite number).
    """
    goal = target["target"]
    deviation = _finite_or_none(lambda: abs(value - goal))
    scale, basis = target_scale(target)
    if not (math.isfinite(scale) and scale >= sys.float_info.min):
        raise RequestError("INVALID_REQUEST", f"the target scale ({basis}) must be a normal positive "
                           "number", pointer=pointer, hint="give a larger tolerance",
                           details={"scale_basis": basis})
    normalized = None if deviation is None else _finite_or_none(lambda: deviation / scale)
    if normalized is None:
        raise RequestError("INVALID_REQUEST", f"the normalized deviation of {target.get('fact')} "
                           f"({'overflow' if deviation is None else repr(deviation)} / {scale!r}) "
                           "is not a finite number", pointer=pointer,
                           hint="give a larger tolerance or relative_tolerance",
                           details={"deviation": deviation, "scale": scale, "scale_basis": basis})
    return {"value": value, "target": goal, "deviation": deviation, "scale": scale,
            "scale_basis": basis, "normalized": normalized}


def preference_value(view: Mapping[str, Any], preference: Mapping[str, Any], ctx: EvalContext, *,
                     pointer: str | None = None) -> tuple[float | None, str | None, dict[str, Any]]:
    """The value a preference ranks by, or an unknown with its reason.

    Shorthand `{fact, direction, conditions?, select?, path?, required_sampled_field_fractions?}`
    resolves like a target. The fixture
    form `{scope, metric, direction, <pins>}` takes `view[scope][metric]` only when every pin equals
    the cost object's member (`scope_of_cost` pins the object's `scope`); otherwise
    PREFERENCE_NOT_COMPARABLE with per-pin expected/actual.
    """
    if not is_fixture_preference(preference):
        return _single_numeric(view, preference, ctx, pointer)
    scope = list(preference["scope"])
    metric = preference["metric"]
    info: dict[str, Any] = {"fact": ".".join(scope + [metric]),
                            "json_path": ".".join(scope + [metric])}
    not_fact = _not_fact_hit(scope + [metric])
    if not_fact is not None:
        info["details"] = not_fact
        return None, COMPARISON_ONLY, info
    stop = stop_status(view)
    if _prefix_stop_dependent(scope + [metric], ctx) is not None and not stop["eligible"]:
        info["details"] = {"state": stop["state"]}
        return None, STOP_NOT_ELIGIBLE, info
    obj = _at(view, scope)
    if not isinstance(obj, dict):
        return None, FACT_MISSING, info
    # Amendment 10.9: the ranking value passes the same discovery-only and coverage guards as
    # the shorthand form (steps 1-6 and 10.2).
    walked = scope + [metric]
    node: object = view
    for depth, segment in enumerate(walked, start=1):
        node = node.get(segment) if isinstance(node, dict) else None
        hit = _discovery_hit(".".join(walked[:depth]), node)
        if hit:
            info["details"] = hit
            return None, DISCOVERY_ONLY, info
    coverage = _coverage_guard(view, walked)
    if coverage is not None:
        info["details"] = coverage
        return None, COVERAGE_UNCONFIRMED, info
    differs = {}
    for key, expected in preference.items():
        if key in ("id", "scope", "metric", "direction"):
            continue
        member = _PREFERENCE_PIN_TARGETS.get(key, key)
        if member not in obj or not json_equal(obj[member], expected):
            differs[key] = _difference(obj, member, expected)
    if differs:
        info["details"] = {"differs": differs}
        return None, PREFERENCE_NOT_COMPARABLE, info
    value = obj.get(metric)
    info["sampled_field_fractions"] = _sampling_grid(obj)
    conditions = obj.get("conditions")
    info["conditions"] = conditions if isinstance(conditions, dict) else None
    info["value_member"] = metric
    info["unit"] = unit_for_name(metric)
    info["basis"] = obj.get("basis") if isinstance(obj.get("basis"), str) else None
    if value is None:
        return None, VALUE_MISSING, info
    if not _is_number(value):
        return None, VALUE_TYPE, info
    return value, None, info


def preference_rank_entry(value: float | None, direction: str) -> list[Any]:
    """A preference's contribution to `rank.key` (amendment 10.9): `[0, v]` ascending,
    `[0, -v]` descending, `[1, null]` unknown (sorts last)."""
    if value is None:
        return [1, None]
    return [0, value if direction == "ascending" else -value]

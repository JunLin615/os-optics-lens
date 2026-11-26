"""Record normalization: configuration views, facets and typed facts.

A *configuration view* is the JSON that constraint evaluation reads for one (record, configuration)
unit. Typed facts and configuration-level facets are derived from the same view, so the facts table
can never disagree with constraint evaluation.

Configuration resolution for record schema 1.1 applies whole-section overrides before the corpus
loader projects the result onto Project facts. `conjugate` replaces `design.conjugate`. The
`configuration` member holds the native id, label and native flag. Without record overrides, one
view represents the Project as written, taking the native id if the Project declares exactly one.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any, Iterable

from .canonical import canonical_json, canonical_key, is_number
from .vocabulary import MAPPING_BASIS, RECORD_BASIS, Vocabulary, load_vocabulary

PROJECT_AS_WRITTEN = "project as written"
CONFIGURATION_ENTRY_KEYS = frozenset({
    "configuration_id", "label", "conjugate", "first_order", "aperture", "field", "image_format",
    "spectrum", "performance", "analysis_evidence", "cost_proxy",
})
"""Keys allowed in a record `configurations[]` entry."""
CONFIGURATION_SECTIONS = ("analysis_evidence", "aperture", "cost_proxy", "field", "first_order",
                          "image_format", "performance", "spectrum")
"""Whole sections a configuration entry may replace."""
PER_CONFIGURATION_REQUIRED = ("first_order", "aperture", "field", "performance")
"""Sections that, with `configurations`, must be absent at top level or overridden by every entry."""
NATIVE_CONFIGURATION_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")
RESERVED_DEVICE_NAMES = frozenset(
    ["con", "prn", "aux", "nul"] + [f"com{i}" for i in range(1, 10)] + [f"lpt{i}" for i in range(1, 10)]
)

UNIT_SUFFIXES = (("_lp_mm", "lp/mm"), ("_percent", "percent"), ("_deg", "deg"), ("_um", "um"),
                 ("_nm", "nm"), ("_mm", "mm"))
DIMENSIONLESS_ENDINGS = ("f_number", "numerical_aperture", "_count", "_fraction", "_weight")
FROM_SUFFIX = re.compile(r"_from_[a-z0-9]+$")
VALUE_FIELD = re.compile(r"value(_[a-z_]+)?")
BASIS_CLASSES = ("PROJECT_DECLARED", "PROJECT_DERIVED", "DERIVED", "ENGINE_COMPUTED")
PERFORMANCE_NON_QUALIFIERS = frozenset({
    "metric", "basis", "engine_version", "source", "condition", "conditions", "definition",
    "limiting_sample", "sampled_field_fractions",
})


# Configurations ------------------------------------------------------------------------------


@dataclass(frozen=True)
class NativeConfiguration:
    """One configuration declared by the native Project (`configurations[]`)."""

    configuration_id: str
    label: str | None


@dataclass(frozen=True)
class Issue:
    """A normalization problem; the corpus loader turns it into a `Finding` for the record's file.

    `field` is a dotted path into record.json (or into project.json when `in_project` is true).
    """

    severity: str
    code: str
    field: str
    message: str
    in_project: bool = False


@dataclass
class ConfigView:
    """One indexed configuration of a record.

    `configuration_key` is the SQLite key ('' for the Project as written); `configuration_id` is the
    native id or None; `ordinal` is the 0-based position used for deterministic ordering.
    """

    configuration_key: str
    configuration_id: str | None
    native: bool
    label: str
    ordinal: int
    view: dict[str, Any]


def native_configurations(project: Any) -> tuple[list[NativeConfiguration], list[Issue]]:
    """The valid native configurations a Project declares, and problems with the declaration.

    Ids must match `^[A-Za-z][A-Za-z0-9_-]{0,63}$`, be unique case-insensitively and not be a reserved
    device name; violations are PROJECT_CONFIGURATION_INVALID errors and the offending entry is
    dropped. A Project without `configurations` declares none.
    """
    if not isinstance(project, dict) or "configurations" not in project or project["configurations"] is None:
        return [], []
    declared = project["configurations"]
    if not isinstance(declared, list):
        return [], [Issue("error", "PROJECT_CONFIGURATION_INVALID", "configurations",
                          "the Project's configurations must be a list", True)]
    natives: list[NativeConfiguration] = []
    issues: list[Issue] = []
    seen: dict[str, str] = {}
    for index, entry in enumerate(declared):
        where = f"configurations[{index}]"
        cid = entry.get("configuration_id") if isinstance(entry, dict) else None
        if not isinstance(cid, str):
            issues.append(Issue("error", "PROJECT_CONFIGURATION_INVALID", f"{where}.configuration_id",
                                "a Project configuration needs a string configuration_id", True))
            continue
        if NATIVE_CONFIGURATION_ID.fullmatch(cid) is None:
            issues.append(Issue("error", "PROJECT_CONFIGURATION_INVALID", f"{where}.configuration_id",
                                f"configuration id {cid!r} must start with a letter and contain only letters, "
                                "digits, '_' or '-' (at most 64 characters)", True))
            continue
        if cid.lower() in RESERVED_DEVICE_NAMES:
            issues.append(Issue("error", "PROJECT_CONFIGURATION_INVALID", f"{where}.configuration_id",
                                f"configuration id {cid!r} is a reserved device name", True))
            continue
        if cid.lower() in seen:
            issues.append(Issue("error", "PROJECT_CONFIGURATION_INVALID", f"{where}.configuration_id",
                                f"configuration id {cid!r} duplicates {seen[cid.lower()]!r} (ids are "
                                "compared case-insensitively)", True))
            continue
        seen[cid.lower()] = cid
        for member in ("label", "name"):
            if entry.get(member) is not None and not isinstance(entry[member], str):
                issues.append(Issue("error", "PROJECT_CONFIGURATION_INVALID", f"{where}.{member}",
                                    f"a Project configuration's {member} must be a string", True))
        label = entry.get("label") if isinstance(entry.get("label"), str) else None
        if label is None and isinstance(entry.get("name"), str):
            label = entry["name"]
        natives.append(NativeConfiguration(cid, label))
    return natives, issues


def check_configurations(record: dict[str, Any], natives: list[NativeConfiguration]) -> list[Issue]:
    """Validate a record's `configurations` against its schema version and the native Project.

    Codes: INVALID_FIELD (1.0 record with configurations, malformed entries, unknown entry keys,
    top-level sections not overridden by every entry), MISSING_FIELD, CONFIGURATION_UNKNOWN,
    DUPLICATE_CONFIGURATION_ID (case-insensitive), CONFIGURATION_UNMAPPED (Project declares two or
    more configurations and the record maps none).
    """
    issues: list[Issue] = []
    if "configurations" not in record:
        if len(natives) >= 2:
            ids = ", ".join(n.configuration_id for n in natives)
            issues.append(Issue("error", "CONFIGURATION_UNMAPPED", "configurations",
                                f"the native Project declares {len(natives)} configurations ({ids}); the "
                                "record must map them in configurations[]"))
        return issues
    entries = record["configurations"]
    if record.get("record_schema_version") == "1.0":
        return [Issue("error", "INVALID_FIELD", "configurations",
                      "configurations requires record_schema_version 1.1")]
    if not isinstance(entries, list) or not entries:
        return [Issue("error", "INVALID_FIELD", "configurations",
                      "configurations must be a non-empty list of objects")]
    declared = {n.configuration_id for n in natives}
    seen: dict[str, str] = {}
    for index, entry in enumerate(entries):
        where = f"configurations[{index}]"
        if not isinstance(entry, dict):
            issues.append(Issue("error", "INVALID_FIELD", where, "a configuration entry must be an object"))
            continue
        for key in sorted(set(entry) - CONFIGURATION_ENTRY_KEYS):
            issues.append(Issue("error", "INVALID_FIELD", f"{where}.{key}",
                                f"{key!r} is not allowed in a configuration entry; allowed: "
                                + ", ".join(sorted(CONFIGURATION_ENTRY_KEYS))))
        cid = entry.get("configuration_id")
        if "configuration_id" not in entry:
            issues.append(Issue("error", "MISSING_FIELD", f"{where}.configuration_id",
                                "a configuration entry needs configuration_id"))
        elif not isinstance(cid, str):
            issues.append(Issue("error", "INVALID_FIELD", f"{where}.configuration_id",
                                "configuration_id must be a string"))
        else:
            if cid.lower() in seen:
                issues.append(Issue("error", "DUPLICATE_CONFIGURATION_ID", f"{where}.configuration_id",
                                    f"configuration id {cid!r} duplicates {seen[cid.lower()]!r}"))
            else:
                seen[cid.lower()] = cid
            if cid not in declared:
                listed = ", ".join(sorted(declared)) or "none"
                issues.append(Issue("error", "CONFIGURATION_UNKNOWN", f"{where}.configuration_id",
                                    f"configuration id {cid!r} is not declared by the native Project "
                                    f"(declared: {listed})"))
        if "label" in entry and not isinstance(entry["label"], str):
            issues.append(Issue("error", "INVALID_FIELD", f"{where}.label", "label must be a string"))
        if "conjugate" in entry and not isinstance(entry["conjugate"], str):
            issues.append(Issue("error", "INVALID_FIELD", f"{where}.conjugate", "conjugate must be a string"))
        for section in CONFIGURATION_SECTIONS:
            if section not in entry:
                continue
            expected_type = list if section == "performance" else dict
            if not isinstance(entry[section], expected_type):
                issues.append(Issue("error", "INVALID_FIELD", f"{where}.{section}",
                                    f"{section} must be {'a list' if expected_type is list else 'an object'}"))
    dict_entries = [entry for entry in entries if isinstance(entry, dict)]
    for section in PER_CONFIGURATION_REQUIRED:
        if section in record and not all(section in entry for entry in dict_entries):
            issues.append(Issue("error", "INVALID_FIELD", "configurations",
                                f"top-level {section} would be inherited by some configurations; move it into "
                                "every configuration entry or override it in each"))
    design = record.get("design")
    if isinstance(design, dict) and "conjugate" in design and not all("conjugate" in e for e in dict_entries):
        issues.append(Issue("error", "INVALID_FIELD", "configurations",
                            "design.conjugate would be inherited by some configurations; give conjugate in "
                            "every configuration entry or remove it from design"))
    return issues


def configuration_views(record: dict[str, Any], project: Any) -> list[ConfigView]:
    """Resolve each configuration's metadata in ordinal order, before Project fact projection.

    `project` is the parsed native Project (or None). Malformed entries are skipped.
    """
    natives, _ = native_configurations(project)
    native_labels = {n.configuration_id: n.label for n in natives}
    base = {key: value for key, value in record.items() if key != "configurations"}
    entries = record.get("configurations")
    if isinstance(entries, list) and entries:
        views: list[ConfigView] = []
        for ordinal, entry in enumerate(entries):
            if not isinstance(entry, dict) or not isinstance(entry.get("configuration_id"), str):
                continue
            cid = entry["configuration_id"]
            view = copy.deepcopy(base)
            for key in sorted(entry):
                if key in ("configuration_id", "label"):
                    continue
                if key == "conjugate":
                    design = view.get("design")
                    if not isinstance(design, dict):
                        design = view["design"] = {}
                    design["conjugate"] = copy.deepcopy(entry[key])
                else:
                    view[key] = copy.deepcopy(entry[key])
            label = entry.get("label") if isinstance(entry.get("label"), str) else None
            label = label or native_labels.get(cid) or cid
            view["configuration"] = {"configuration_id": cid, "label": label, "native": True}
            views.append(ConfigView(cid, cid, True, label, ordinal, view))
        return views
    view = copy.deepcopy(base)
    if len(natives) == 1:
        native = natives[0]
        label = native.label or native.configuration_id
        view["configuration"] = {"configuration_id": native.configuration_id, "label": label, "native": True}
        return [ConfigView(native.configuration_id, native.configuration_id, True, label, 0, view)]
    view["configuration"] = {"configuration_id": None, "label": PROJECT_AS_WRITTEN, "native": False}
    return [ConfigView("", None, False, PROJECT_AS_WRITTEN, 0, view)]


# Units and bases ------------------------------------------------------------------------------


def unit_for(name: str) -> str | None:
    """The unit implied by a member name (amendment 10.8).

    A name containing `_fraction_of_` is a ratio, so dimensionless ("1"). Otherwise a trailing
    `_from_<word>` is stripped, then the longest unit suffix wins (`_lp_mm` -> lp/mm, `_percent`,
    `_deg`, `_um`, `_nm`, `_mm`); names ending in f_number, numerical_aperture, `_count`,
    `_fraction` or `_weight` are dimensionless ("1"); otherwise None.
    """
    if "_fraction_of_" in name:
        return "1"
    stripped = FROM_SUFFIX.sub("", name)
    for suffix, unit in UNIT_SUFFIXES:
        if stripped.endswith(suffix):
            return unit
    if stripped.endswith(DIMENSIONLESS_ENDINGS):
        return "1"
    return None


def member_stem(name: str) -> str:
    """`name` without a trailing `_from_<word>` and without its unit suffix."""
    stripped = FROM_SUFFIX.sub("", name)
    for suffix, _ in UNIT_SUFFIXES:
        if stripped.endswith(suffix):
            return stripped[: -len(suffix)]
    return stripped


def performance_value_unit(name: str) -> str | None:
    """Unit of a performance value field: `value` -> "1", `value_<suffix>`/`min_<suffix>` by suffix."""
    if name == "value":
        return "1"
    return unit_for(name)


def basis_class(basis: str | None) -> str | None:
    """The leading token of a basis string when it is SOURCE_STATED, DERIVED or ENGINE_COMPUTED, else
    OTHER; None when there is no basis."""
    if basis is None:
        return None
    match = re.match(r"[A-Z_]+", basis.strip())
    token = match.group(0) if match else ""
    return token if token in BASIS_CLASSES else "OTHER"


def _named_in(text: Any, member: str) -> bool:
    if not isinstance(text, str):
        return False
    haystack = text.lower()
    stem = member_stem(member)
    return any(form and form in haystack for form in (member, stem, stem.replace("_", " ")))


# Fact extraction ------------------------------------------------------------------------------


def _scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, bool)) or is_number(value)


def _value_member(entry: dict[str, Any]) -> str | None:
    """`value` if present, else the first (sorted) `value_<suffix>` member, else None."""
    if "value" in entry:
        return "value"
    candidates = sorted(key for key in entry if VALUE_FIELD.fullmatch(key) and not key.endswith("_basis"))
    return candidates[0] if candidates else None


def _interval_pair(obj: dict[str, Any]) -> tuple[str, str] | None:
    """The first (sorted) `min_<s>`/`max_<s>` member pair of an object, or None."""
    for key in sorted(obj):
        if key.startswith("min_") and f"max_{key[4:]}" in obj:
            return key, f"max_{key[4:]}"
    return None


def _natural_key(path: str) -> tuple[Any, ...]:
    parts: list[Any] = []
    for token in re.split(r"(\[\d+\])", path):
        if token.startswith("[") and token.endswith("]"):
            parts.append((1, int(token[1:-1])))
        elif token:
            parts.append((0, token))
    return tuple(parts)


class _FactBuilder:
    """Collects facts for one configuration view according to spec §4 and amendments 10.7/10.8."""

    def __init__(self, view: dict[str, Any], vocabulary: Vocabulary) -> None:
        self.view = view
        rules = vocabulary.fact_extraction
        self.fact_sections = set(rules.get("fact_sections", []))
        self.special_sections = set(rules.get("special_sections", []))
        self.non_fact_sections = set(rules.get("non_fact_sections", []))
        self.check_members = set(rules.get("check_members", []))
        self.skip_members = set(rules.get("skip_members", []))
        self.skip_suffixes = tuple(rules.get("skip_suffixes", []))
        self.facts: list[dict[str, Any]] = []

    def skipped(self, name: str) -> bool:
        return name in self.skip_members or name in self.check_members or name.endswith(self.skip_suffixes)

    def add(self, **fields: Any) -> None:
        fact: dict[str, Any] = {
            "fact_key": None, "json_path": None, "metric": None, "value_num": None, "value_min": None,
            "value_max": None, "value_json": None, "unit": None, "basis": None, "basis_class": None,
            "conditions_json": None, "conditions_text": None, "definition": None, "eligible_for": None,
            "state": None, "system_aperture": None, "qualifiers_json": None, "value_paths": None,
            "constraint_form": None, "sampled_field_fractions_json": None, "source": None, "source_field": None,
        }
        fact.update(fields)
        fact["basis_class"] = basis_class(fact["basis"])
        self.facts.append(fact)

    def run(self) -> list[dict[str, Any]]:
        for section in sorted(self.view):
            content = self.view[section]
            if section in self.non_fact_sections:
                continue
            if section == "spectrum" and isinstance(content, dict):
                self.section(section, content)
            elif section == "performance" and isinstance(content, list):
                self.performance(content)
            elif section == "analysis_evidence" and isinstance(content, dict):
                self.analysis_evidence(content)
            elif section not in self.special_sections and isinstance(content, dict):
                self.section(section, content)
        self.facts.sort(key=lambda fact: (fact["fact_key"], _natural_key(fact["json_path"])))
        return self.facts

    # Section members (rules 1, 2, 3, 5 and 10.8) --------------------------------------------

    def section_context(self, section: str, content: dict[str, Any], member: str) -> tuple[str | None, str | None]:
        """Section-level basis and definition that apply to `member` (amendment 10.7)."""
        numeric_members = [
            name for name, value in content.items()
            if not self.skipped(name) and (is_number(value) or (isinstance(value, dict) and is_number(value.get("value"))))
        ]
        applies = numeric_members == [member] or _named_in(content.get("definition"), member)
        if not applies:
            return None, None
        basis = content.get("basis") if isinstance(content.get("basis"), str) else None
        definition = content.get("definition") if isinstance(content.get("definition"), str) else None
        return basis, definition

    def member_basis(self, section: str, content: dict[str, Any], member: str, own: Any) -> str | None:
        if isinstance(own, str):
            return own
        sibling = content.get(f"{member_stem(member)}_basis")
        if isinstance(sibling, str):
            return sibling
        return self.section_context(section, content, member)[0]

    def section_state(self, section: str, own: Any) -> str | None:
        if isinstance(own, str):
            return own
        if section == "field":
            verification = self.view.get("field", {}).get("verification")
            if isinstance(verification, dict) and isinstance(verification.get("state"), str):
                return verification["state"]
        return None

    def section(self, section: str, content: dict[str, Any]) -> None:
        # A scalar member resolves to its section object, so the section's grid (if any) is its grid.
        grid = _grid(content)
        for member in sorted(content):
            value = content[member]
            if self.skipped(member):
                continue
            key = f"{section}.{member}"
            if isinstance(value, dict):
                self.object_member(section, content, member, value)
            elif is_number(value):
                basis = self.member_basis(section, content, member, None)
                definition = self.section_context(section, content, member)[1]
                self.add(fact_key=key, json_path=key, value_num=float(value), value_json=canonical_json(value),
                         unit=unit_for(member), basis=basis, definition=definition,
                         state=self.section_state(section, None),
                         constraint_form=_form([section], None, member), **grid)
            elif isinstance(value, list):
                if value and all(_scalar(item) and item is not None for item in value):
                    basis = self.member_basis(section, content, member, None)
                    definition = self.section_context(section, content, member)[1]
                    self.add(fact_key=key, json_path=key, value_json=canonical_json(value),
                             unit=unit_for(member), basis=basis, definition=definition,
                             state=self.section_state(section, None),
                             constraint_form=_form([section], None, member), **grid)
            elif isinstance(value, (str, bool)):
                basis = self.member_basis(section, content, member, None)
                self.add(fact_key=key, json_path=key, value_json=canonical_json(value), basis=basis,
                         state=self.section_state(section, None),
                         constraint_form=_form([section], None, member), **grid)

    def object_member(self, section: str, content: dict[str, Any], member: str, obj: dict[str, Any]) -> None:
        key = f"{section}.{member}"
        common = {
            "basis": self.member_basis(section, content, member, obj.get("basis")),
            "definition": _prose(obj),
            "eligible_for": obj.get("eligible_for") if isinstance(obj.get("eligible_for"), str) else None,
            "state": self.section_state(section, obj.get("state")),
            "system_aperture": obj.get("system_aperture") if isinstance(obj.get("system_aperture"), bool) else None,
            "source": obj.get("source") if isinstance(obj.get("source"), dict) else None,
            "source_field": f"{key}.source" if isinstance(obj.get("source"), dict) else None,
            **_conditions(obj),
            **_grid(obj),
        }
        if "value" in obj:
            value = obj["value"]
            if not is_number(value):
                return
            self.add(fact_key=key, json_path=f"{key}.value", value_num=float(value),
                     value_json=canonical_json(value), unit=unit_for(member),
                     value_paths=canonical_json(["value"]), constraint_form=_form([section, member], None, "value"),
                     **common)
            return
        pair = _interval_pair(obj)
        if pair is not None:
            low, high = obj.get(pair[0]), obj.get(pair[1])
            self.add(fact_key=key, json_path=key,
                     value_min=float(low) if is_number(low) else None,
                     value_max=float(high) if is_number(high) else None,
                     value_json=canonical_json([low, high]), unit=unit_for(pair[0]) or unit_for(member),
                     value_paths=canonical_json(list(pair)), constraint_form=_form([section, member], None, ""),
                     **common)
            return
        if obj and all(_scalar(item) for item in obj.values()):
            for sub in sorted(obj):
                item = obj[sub]
                if self.skipped(sub) or item is None:
                    continue
                sub_key = f"{key}.{sub}"
                basis = obj.get("basis") if isinstance(obj.get("basis"), str) else common["basis"]
                if is_number(item):
                    self.add(fact_key=sub_key, json_path=sub_key, value_num=float(item),
                             value_json=canonical_json(item), unit=unit_for(sub), basis=basis,
                             state=self.section_state(section, None),
                             constraint_form=_form([section, member], None, sub))
                else:
                    self.add(fact_key=sub_key, json_path=sub_key, value_json=canonical_json(item), basis=basis,
                             state=self.section_state(section, None),
                             constraint_form=_form([section, member], None, sub))

    # Performance (rule 4, 10.7) --------------------------------------------------------------

    def performance(self, entries: list[Any]) -> None:
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict) or not isinstance(entry.get("metric"), str):
                continue
            metric = entry["metric"]
            path = f"performance[{index}]"
            value_member = _value_member(entry)
            pair = _interval_pair(entry)
            value_like = sorted(
                name for name, item in entry.items()
                if is_number(item) and name not in PERFORMANCE_NON_QUALIFIERS and not name.endswith("_basis")
                and (VALUE_FIELD.fullmatch(name) or name.startswith(("min_", "max_")) or unit_for(name) is None)
            )
            qualifiers = {
                name: item for name, item in entry.items()
                if name not in PERFORMANCE_NON_QUALIFIERS and name not in value_like
                and not name.endswith("_basis") and not isinstance(item, dict)
                and not (VALUE_FIELD.fullmatch(name) or (pair is not None and name in pair))
            }
            fields: dict[str, Any] = {
                "fact_key": f"performance.{metric}",
                "metric": metric,
                "basis": entry.get("basis") if isinstance(entry.get("basis"), str) else None,
                "definition": _prose(entry),
                "eligible_for": entry.get("eligible_for") if isinstance(entry.get("eligible_for"), str) else None,
                "state": entry.get("state") if isinstance(entry.get("state"), str) else None,
                "source": entry.get("source") if isinstance(entry.get("source"), dict) else None,
                "source_field": f"{path}.source" if isinstance(entry.get("source"), dict) else None,
                "qualifiers_json": canonical_key(qualifiers) if qualifiers else None,
                "value_paths": canonical_json(value_like) if value_like else None,
                **_conditions(entry),
                **_grid(entry),
            }
            if value_member is not None and is_number(entry.get(value_member)):
                value = entry[value_member]
                self.add(json_path=f"{path}.{value_member}", value_num=float(value), value_json=canonical_json(value),
                         unit=performance_value_unit(value_member),
                         constraint_form=_form(["performance"], metric, value_member), **fields)
            elif pair is not None:
                low, high = entry.get(pair[0]), entry.get(pair[1])
                self.add(json_path=path, value_min=float(low) if is_number(low) else None,
                         value_max=float(high) if is_number(high) else None,
                         value_json=canonical_json([low, high]), unit=performance_value_unit(pair[0]),
                         constraint_form=_form(["performance"], metric, None), **fields)
            else:
                self.add(json_path=path, constraint_form=_form(["performance"], metric, None), **fields)

    # Analysis evidence (rule 5) --------------------------------------------------------------

    def analysis_evidence(self, content: dict[str, Any]) -> None:
        for study in sorted(content):
            obj = content[study]
            if not isinstance(obj, dict):
                continue
            key = f"analysis_evidence.{study}"
            self.add(fact_key=key, json_path=key, value_json=canonical_json(obj),
                     basis=obj.get("basis") if isinstance(obj.get("basis"), str) else None,
                     definition=_prose(obj),
                     state=obj.get("status") if isinstance(obj.get("status"), str) else None,
                     source=obj.get("source") if isinstance(obj.get("source"), dict) else None,
                     source_field=f"{key}.source" if isinstance(obj.get("source"), dict) else None,
                     constraint_form=_form(["analysis_evidence", study], None, None), **_grid(obj))


def _prose(obj: dict[str, Any]) -> str | None:
    parts = [obj[key] for key in ("definition", "condition") if isinstance(obj.get(key), str)]
    return "\n".join(parts) if parts else None


def _conditions(obj: dict[str, Any]) -> dict[str, Any]:
    conditions = obj.get("conditions")
    if isinstance(conditions, dict):
        return {"conditions_json": canonical_key(conditions), "conditions_text": None}
    if isinstance(conditions, str):
        return {"conditions_json": None, "conditions_text": conditions}
    return {"conditions_json": None, "conditions_text": None}


def _grid(obj: dict[str, Any]) -> dict[str, Any]:
    """The sampling grid (`sampled_field_fractions`) of the object a fact resolves to: a
    comparison condition of its values."""
    grid = obj.get("sampled_field_fractions")
    return {"sampled_field_fractions_json": None if grid is None else canonical_key(grid)}


def _form(scope: list[str], metric: str | None, path: str | None) -> str:
    return canonical_json({"scope": scope, "metric": metric, "path": path})


def extract_facts(view: dict[str, Any], vocabulary: Vocabulary | None = None) -> list[dict[str, Any]]:
    """Typed facts of one configuration view, sorted by (fact_key, json_path).

    Each fact is a dict with the facts-table columns: fact_key, json_path, metric, value_num,
    value_min, value_max, value_json, unit, basis, basis_class, conditions_json, conditions_text,
    definition, eligible_for, state, system_aperture, qualifiers_json, value_paths (JSON text),
    constraint_form (JSON text), sampled_field_fractions_json (the sampling grid), plus `source` (the fact's raw source object or None) and
    `source_field` (the view location of that object, e.g. `performance[4].source`) for linking to an
    evidence reference. JSON columns hold canonical JSON; conditions and qualifiers are normalized
    with `canonical_key` so equal conditions compare equal as text.
    """
    return _FactBuilder(view, vocabulary or load_vocabulary()).run()


# Facets ---------------------------------------------------------------------------------------


@dataclass(frozen=True, order=True)
class FacetValue:
    """One facet assignment with its basis (RECORD or VOCABULARY_MAPPING)."""

    facet: str
    value: str
    basis: str


def _lower(value: Any) -> str | None:
    return value.strip().lower() if isinstance(value, str) and value.strip() else None


def _str_list(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def record_facets(record: dict[str, Any], vocabulary: Vocabulary | None = None) -> tuple[list[FacetValue], list[Issue]]:
    """Record-level facets (collection, family, design_scope, design_type, source_lens_type, function,
    application, structure, module_role, stop_state) and UNKNOWN_TERM warnings."""
    vocab = vocabulary or load_vocabulary()
    values: set[FacetValue] = set()
    issues: list[Issue] = []
    collection = record.get("collection") if isinstance(record.get("collection"), dict) else {}
    design = record.get("design") if isinstance(record.get("design"), dict) else {}
    if isinstance(collection.get("collection_id"), str):
        values.add(FacetValue("collection", collection["collection_id"], RECORD_BASIS))
    if isinstance(design.get("design_family_id"), str):
        values.add(FacetValue("family", design["design_family_id"], RECORD_BASIS))
    scope = _lower(design.get("design_scope"))
    if scope:
        values.add(FacetValue("design_scope", scope, RECORD_BASIS))

    design_type: str | None = None
    lens_type = design.get("lens_type")
    mapped_applications: tuple[str, ...] = ()
    if isinstance(lens_type, str) and lens_type:
        values.add(FacetValue("source_lens_type", lens_type, RECORD_BASIS))
        mapping = vocab.map_lens_type(lens_type)
        if mapping is None:
            issues.append(Issue("warning", "UNKNOWN_TERM", "design.lens_type",
                                f"lens type {lens_type!r} is not in the vocabulary; it is indexed under "
                                "source_lens_type only"))
        else:
            design_type = mapping.design_type
            mapped_applications = mapping.applications
            if design_type:
                basis = RECORD_BASIS if design_type == lens_type else MAPPING_BASIS
                values.add(FacetValue("design_type", design_type, basis))

    for facet, member in (("function", "function_tags"), ("application", "application_tags"),
                          ("structure", "structure_tags")):
        for index, tag in enumerate(_str_list(design.get(member))):
            term, basis, known = vocab.canonical_record_term(facet, tag)
            values.add(FacetValue(facet, term, basis))
            if not known and vocab.warns_unknown(facet):
                issues.append(Issue("warning", "UNKNOWN_TERM", f"design.{member}[{index}]",
                                    f"{facet} term {tag!r} is not in the vocabulary; it is indexed as {term!r}"))
    if "function_tags" not in design and design_type:
        for function in vocab.design_type_functions(design_type):
            values.add(FacetValue("function", function, MAPPING_BASIS))
    present_applications = {v.value for v in values if v.facet == "application"}
    for application in mapped_applications:
        if application not in present_applications:
            values.add(FacetValue("application", application, MAPPING_BASIS))

    module_role = design.get("module_role")
    if isinstance(module_role, str) and module_role.strip():
        term, basis, known = vocab.canonical_record_term("module_role", module_role)
        values.add(FacetValue("module_role", term if known else module_role, basis))
        if not known and vocab.warns_unknown("module_role"):
            issues.append(Issue("warning", "UNKNOWN_TERM", "design.module_role",
                                f"module_role term {module_role!r} is not in the vocabulary; it is indexed "
                                "verbatim"))

    validity = design.get("aperture_validity")
    if isinstance(validity, dict):
        state = _lower(validity.get("state"))
        if state:
            values.add(FacetValue("stop_state", state, RECORD_BASIS))
    return sorted(values), issues


def configuration_facets(view: dict[str, Any], vocabulary: Vocabulary | None = None) -> tuple[list[FacetValue], list[Issue]]:
    """Configuration-level facets of one view (band, image_format, conjugate, field_kind,
    system_aperture, evidence) and UNKNOWN_TERM warnings (field paths are record-level paths)."""
    vocab = vocabulary or load_vocabulary()
    values: set[FacetValue] = set()
    issues: list[Issue] = []
    spectrum = view.get("spectrum") if isinstance(view.get("spectrum"), dict) else {}
    image_format = view.get("image_format") if isinstance(view.get("image_format"), dict) else {}
    for facet, section, member, content in (("band", "spectrum", "band_facets", spectrum),
                                            ("image_format", "image_format", "format_facets", image_format)):
        for index, tag in enumerate(_str_list(content.get(member))):
            term, basis, known = vocab.canonical_record_term(facet, tag)
            values.add(FacetValue(facet, term, basis))
            if not known and vocab.warns_unknown(facet):
                issues.append(Issue("warning", "UNKNOWN_TERM", f"{section}.{member}[{index}]",
                                    f"{facet} term {tag!r} is not in the vocabulary; it is indexed as {term!r}"))
    design = view.get("design") if isinstance(view.get("design"), dict) else {}
    conjugate = _lower(design.get("conjugate"))
    if conjugate:
        values.add(FacetValue("conjugate", conjugate, RECORD_BASIS))
    field = view.get("field") if isinstance(view.get("field"), dict) else {}
    kind = _lower(field.get("kind"))
    if kind:
        values.add(FacetValue("field_kind", kind, RECORD_BASIS))
    for name in system_aperture_names(view):
        values.add(FacetValue("system_aperture", name, RECORD_BASIS))
    performance = view.get("performance") if isinstance(view.get("performance"), list) else []
    for entry in performance:
        if isinstance(entry, dict) and isinstance(entry.get("metric"), str):
            values.add(FacetValue("evidence", entry["metric"], RECORD_BASIS))
    analysis = view.get("analysis_evidence") if isinstance(view.get("analysis_evidence"), dict) else {}
    for study, content in analysis.items():
        if isinstance(content, dict):
            values.add(FacetValue("evidence", f"{study}_study", RECORD_BASIS))
    return sorted(values), issues


def system_aperture_names(view: dict[str, Any]) -> list[str]:
    """Names of the view's aperture facts marked `system_aperture: true`, sorted."""
    aperture = view.get("aperture") if isinstance(view.get("aperture"), dict) else {}
    return sorted(name for name, fact in aperture.items()
                  if isinstance(fact, dict) and fact.get("system_aperture") is True)


def iter_source_refs(obj: Any, path: str = "", *, named_source: bool = False) -> Iterable[tuple[str, dict[str, Any]]]:
    """Every fact source reference anywhere in `obj`, with its dotted location (e.g.
    `performance[4].source`), in document order: an object with `path`, `sha256` and `section`
    keys, and every object that is the value of a member named `source` (so a reference missing a
    key is still validated and reported, not skipped)."""
    if isinstance(obj, dict):
        if named_source or ("path" in obj and "sha256" in obj and "section" in obj):
            yield path, obj
        for key, value in obj.items():
            yield from iter_source_refs(value, f"{path}.{key}" if path else key, named_source=key == "source")
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            yield from iter_source_refs(value, f"{path}[{index}]")

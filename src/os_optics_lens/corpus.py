"""Corpus discovery, loading and validation (spec §2 with amendments 10.4, 10.5, 10.21).

`load_corpus(root)` discovers `records/<collection_dir>/<record_dir>/record.json`, validates every
record, its native Project and its evidence references, normalizes valid records into configuration
views, facets and typed facts, and returns everything the index builder writes plus every finding.
It never raises for corpus problems: those are findings. Errors make the build fail; warnings are
stored in the index.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from .canonical import HEX64, JsonFormatError, corpus_relative_path, is_number, lf, loads_strict, record_hash, sha256_hex
from .errors import ERROR, WARNING, Finding, LensError, finding, sort_findings
from .normalize import (
    ConfigView,
    FacetValue,
    Issue,
    NativeConfiguration,
    check_configurations,
    configuration_facets,
    configuration_views,
    extract_facts,
    iter_source_refs,
    native_configurations,
    record_facets,
    system_aperture_names,
)
from .project_facts import PHONE_COLLECTIONS, analysis_applies, project_view
from .vocabulary import Vocabulary, load_vocabulary

RECORD_SCHEMA = "opensci.lens.record"
PROJECT_SCHEMA = "opensci.project"
EVIDENCE_SCHEMA = "opensci.lens.evidence_extract"
SUPPORTED_SCHEMA_VERSIONS = ("1.0", "1.1")
RECORD_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
MAX_REVISION = 2**63 - 1
"""Largest record_revision (the index stores it in an SQLite INTEGER column)."""

RESOLVED = "RESOLVED"
FILE_MISSING = "FILE_MISSING"
HASH_MISMATCH = "HASH_MISMATCH"
SECTION_MISSING = "SECTION_MISSING"
MALFORMED = "MALFORMED"
PATH_ESCAPES = "PATH_ESCAPES"
NOTICE_MISMATCH = "NOTICE_MISMATCH"
"""Evidence reference statuses. NOTICE_MISMATCH applies only to license notice references."""

EVIDENCE_CODES = {
    FILE_MISSING: "EVIDENCE_FILE_MISSING",
    HASH_MISMATCH: "EVIDENCE_HASH_MISMATCH",
    SECTION_MISSING: "EVIDENCE_SECTION_MISSING",
    MALFORMED: "EVIDENCE_MALFORMED",
}
PERFORMANCE_NUMERIC = re.compile(r"(value(_[a-z_]+)?|(min|max)_[a-z_]+)")

OBJECT_SECTIONS = ("aperture", "cost_proxy", "field", "first_order", "image_format", "spectrum")
"""Sections that, when present and not null, must be objects (design is required separately)."""
TEXT_MEMBERS = ("basis", "condition", "definition", "eligible_for", "state")
"""Members of a section or fact object that are indexed as text."""
_UNREADABLE = object()
"""`load_json` result for a file that could not be read or parsed (its finding is already made)."""


def _is_object(value: Any) -> bool:
    return isinstance(value, dict)


def _is_text(value: Any) -> bool:
    return isinstance(value, str)


def _json_type(value: Any) -> str:
    for kind, name in ((bool, "a boolean"), ((int, float), "a number"), (str, "a string"), (list, "a list"),
                       (dict, "an object")):
        if isinstance(value, kind):
            return name
    return "null"


def evidence_extract_problem(data: Any) -> tuple[str, str] | None:
    """(field, problem) when parsed evidence breaks the extract contract of docs/RECORD_FORMAT.md
    (object root, `schema`, a non-empty `sections` object without null members), else None.
    `field` is '' for the whole document."""
    if not isinstance(data, dict):
        return "", f"the root must be a JSON object, not {_json_type(data)}"
    if data.get("schema") != EVIDENCE_SCHEMA:
        return "schema", f"schema must be {EVIDENCE_SCHEMA!r}"
    sections = data.get("sections")
    if not isinstance(sections, dict) or not sections:
        return "sections", "sections must be a non-empty object"
    for name in sorted(sections):
        if sections[name] is None:
            return f"sections.{name}", f"sections.{name} must not be null"
    return None


def non_object_sources(value: Any, path: str) -> Iterator[str]:
    """Dotted locations, at any depth under `value` (found at `path`), of members named `source`
    that are neither null nor an object: broken evidence references that name nothing."""
    if isinstance(value, dict):
        for key, item in value.items():
            where = f"{path}.{key}" if path else key
            if key == "source" and item is not None and not isinstance(item, dict):
                yield where
            else:
                yield from non_object_sources(item, where)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from non_object_sources(item, f"{path}[{index}]")


def evidence_owner_problem(data: dict[str, Any], rid: str) -> str | None:
    """Why the extract `data` cannot be the `collection.evidence_extract` of record `rid` (its
    top-level `record_id` must be that string), else None."""
    owner = data.get("record_id")
    if isinstance(owner, str) and owner == rid:
        return None
    if "record_id" not in data:
        found = "it is missing"
    elif isinstance(owner, str):
        found = f"it is {owner[:128]!r}"
    else:
        found = f"it is {_json_type(owner)}"
    return f"field record_id: a record's collection.evidence_extract must name that record ({rid!r}), but {found}"


@dataclass
class EvidenceRef:
    """One evidence reference of a record.

    `field` is the location of the reference in record.json (`performance[4].source`,
    `collection.evidence_extract`, `collection.license.notice_path`). `path` is the path as written
    (None when not a string). `kind` is `source`, `evidence_extract` or `license_notice`.
    """

    field: str
    kind: str
    path: str | None
    sha256: str | None
    section: str | None
    status: str


@dataclass
class NormalizedRecord:
    """A valid record ready to be indexed."""

    file: str
    record: dict[str, Any]
    record_id: str
    revision: int
    schema_version: str
    title: str
    collection_id: str
    family_id: str | None
    design_scope: str | None
    record_hash: str
    project_path: str
    project_sha256: str
    project_bytes: bytes
    engine_version: str | None
    natives: list[NativeConfiguration]
    views: list[ConfigView]
    record_facets: list[FacetValue]
    config_facets: dict[str, list[FacetValue]]
    facts: dict[str, list[dict[str, Any]]]
    evidence_refs: list[EvidenceRef]


@dataclass
class Corpus:
    """The loaded corpus: indexable records (sorted by record_id), verified evidence files
    (normalized path -> (sha256, LF bytes)), license texts (path -> LF text) and all findings."""

    root: Path
    records: list[NormalizedRecord]
    evidence_files: dict[str, tuple[str, bytes]]
    licenses: dict[str, str]
    findings: list[Finding] = field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        """Error findings."""
        return [item for item in self.findings if item.severity == ERROR]

    @property
    def warnings(self) -> list[Finding]:
        """Warning findings."""
        return [item for item in self.findings if item.severity == WARNING]


def pointer_to_field(pointer: str | None) -> str:
    """Convert a JSON pointer (`/performance/2/value`) to the dotted finding form
    (`performance[2].value`)."""
    if not pointer or pointer == "/":
        return ""
    out = ""
    for token in pointer.lstrip("/").split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if token.isdigit():
            out += f"[{token}]"
        else:
            out += f".{token}" if out else token
    return out


@dataclass
class _FileState:
    """Cached state of a referenced file (keyed by normalized path)."""

    exists: bool
    escapes: bool
    content: bytes | None = None
    sha256: str | None = None
    parsed: Any = None
    problem: str | None = None


class _Loader:
    def __init__(self, root: Path, allow_unresolved_evidence: bool, vocabulary: Vocabulary) -> None:
        self.root = root
        self.resolved_root = root.resolve()
        self.allow = allow_unresolved_evidence
        self.vocab = vocabulary
        self.findings: list[Finding] = []
        self.files: dict[str, _FileState] = {}
        self.evidence_files: dict[str, tuple[str, bytes]] = {}
        self.licenses: dict[str, str] = {}

    # Helpers ------------------------------------------------------------------------------

    def rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def add(self, severity: str, code: str, record_id: str | None, file: str, field_path: str, message: str,
            **compare: Any) -> None:
        self.findings.append(finding(severity, code, record_id, file, field_path, message, **compare))

    def file_state(self, rel_path: str) -> _FileState:
        state = self.files.get(rel_path)
        if state is not None:
            return state
        target = self.root / rel_path
        try:
            resolved = target.resolve()
            escapes = resolved != self.resolved_root and self.resolved_root not in resolved.parents
        except (OSError, RuntimeError):
            escapes = True
        if escapes:
            state = _FileState(exists=False, escapes=True)
        elif not target.is_file():
            state = _FileState(exists=False, escapes=False)
        else:
            content = lf(target.read_bytes())
            state = _FileState(exists=True, escapes=False, content=content, sha256=sha256_hex(content))
        self.files[rel_path] = state
        return state

    def parsed_evidence(self, state: _FileState) -> None:
        """Parse an evidence file once; sets `parsed` (a conforming evidence extract) or `problem`."""
        if state.parsed is not None or state.problem is not None or state.content is None:
            return
        try:
            data = loads_strict(state.content, reject_duplicate_keys=True)
        except JsonFormatError as exc:
            state.problem = exc.message
            return
        broken = evidence_extract_problem(data)
        if broken is not None:
            name, problem = broken
            state.problem = f"field {name}: {problem}" if name else problem
            return
        state.parsed = data

    # Discovery ----------------------------------------------------------------------------

    def discover(self) -> tuple[list[Path], dict[str, list[Path]]]:
        records_dir = self.root / "records"
        record_files: list[Path] = []
        manifests: dict[str, list[Path]] = {}
        for collection_dir in sorted(p for p in records_dir.iterdir() if p.is_dir()):
            found = sorted(p for p in collection_dir.glob("batch-*.json") if p.is_file())
            if found:
                manifests[collection_dir.name] = found
            for record_dir in sorted(p for p in collection_dir.iterdir() if p.is_dir()):
                candidate = record_dir / "record.json"
                if candidate.is_file():
                    record_files.append(candidate)
        record_files.sort(key=lambda p: self.rel(p))
        return record_files, manifests

    # Record validation --------------------------------------------------------------------

    def load_json(self, path: Path, record_id: str | None = None) -> Any:
        file = self.rel(path)
        try:
            return loads_strict(path.read_bytes(), reject_duplicate_keys=True)
        except JsonFormatError as exc:
            self.add(ERROR, "MALFORMED_JSON", record_id, file, pointer_to_field(exc.pointer), exc.message)
        except OSError as exc:
            self.add(ERROR, "MALFORMED_JSON", record_id, file, "", f"cannot read file: {exc.strerror}")
        return _UNREADABLE

    def require(self, record: dict[str, Any], dotted: str, check, expectation: str, rid: str | None,
                file: str, missing_severity: str = ERROR, missing_code: str = "MISSING_FIELD") -> Any:
        """Check a required field; returns its value when valid, else None (with a finding)."""
        container: Any = record
        parts = dotted.split(".")
        for part in parts[:-1]:
            container = container.get(part) if isinstance(container, dict) else None
        if not isinstance(container, dict) or parts[-1] not in container:
            self.add(missing_severity, missing_code, rid, file, dotted, f"{dotted} is required ({expectation})")
            return None
        value = container[parts[-1]]
        if not check(value):
            self.add(ERROR, "INVALID_FIELD", rid, file, dotted, f"{dotted} must be {expectation}",
                     expected=expectation, actual=value)
            return None
        return value

    def validate_record(self, record: dict[str, Any], file: str) -> str | None:
        """Required-field checks (§2.2). Returns the record id when it is valid."""
        rid_value = record.get("record_id")
        rid = rid_value if isinstance(rid_value, str) else None
        if "schema" not in record:
            self.add(ERROR, "MISSING_FIELD", rid, file, "schema", f"schema is required ({RECORD_SCHEMA!r})")
        elif record["schema"] != RECORD_SCHEMA:
            self.add(ERROR, "INVALID_FIELD", rid, file, "schema", f"schema must be {RECORD_SCHEMA!r}",
                     expected=RECORD_SCHEMA, actual=record["schema"])
        if "record_schema_version" not in record:
            self.add(ERROR, "MISSING_FIELD", rid, file, "record_schema_version",
                     "record_schema_version is required (" + " or ".join(SUPPORTED_SCHEMA_VERSIONS) + ")")
        elif record["record_schema_version"] not in SUPPORTED_SCHEMA_VERSIONS:
            self.add(ERROR, "UNSUPPORTED_SCHEMA_VERSION", rid, file, "record_schema_version",
                     f"record_schema_version {record['record_schema_version']!r} is not supported",
                     expected=list(SUPPORTED_SCHEMA_VERSIONS), actual=record["record_schema_version"])
        valid_id = self.require(record, "record_id", lambda v: isinstance(v, str) and RECORD_ID.fullmatch(v),
                                "a string matching ^[a-z0-9][a-z0-9._-]{0,127}$", rid, file)
        self.require(record, "record_revision",
                     lambda v: isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= MAX_REVISION,
                     f"an integer from 1 to {MAX_REVISION}", rid, file)
        self.require(record, "title", lambda v: isinstance(v, str), "a string", rid, file)
        self.require(record, "collection", lambda v: isinstance(v, dict), "an object", rid, file)
        self.require(record, "collection.collection_id", lambda v: isinstance(v, str) and v, "a non-empty string",
                     rid, file)
        if self.require(record, "design", lambda v: isinstance(v, dict), "an object", rid, file) is not None:
            self.require(record, "design.design_family_id", lambda v: isinstance(v, str) and v,
                         "a non-empty string", rid, file, missing_severity=WARNING, missing_code="MISSING_FAMILY")
            design = record["design"]
            for member in ("lens_type", "design_scope", "conjugate"):
                self.optional(design, member, _is_text, "a string or null", f"design.{member}", rid, file)
            self.optional(design, "aperture_validity", _is_object, "an object or null", "design.aperture_validity",
                          rid, file)
            if "module_role" in design and not (isinstance(design["module_role"], str)
                                                and design["module_role"].strip()):
                self.add(ERROR, "INVALID_FIELD", rid, file, "design.module_role",
                         "design.module_role must be a non-empty string")
            for member in ("function_tags", "application_tags", "structure_tags"):
                self.check_string_list(design, member, f"design.{member}", rid, file)
            if "configuration_count" in design and not (
                    isinstance(design["configuration_count"], int) and not isinstance(design["configuration_count"], bool)):
                self.add(ERROR, "INVALID_FIELD", rid, file, "design.configuration_count",
                         "design.configuration_count must be an integer")
        self.require(record, "native_project", lambda v: isinstance(v, dict), "an object", rid, file)
        self.require(record, "native_project.path", lambda v: isinstance(v, str) and v, "a non-empty string", rid, file)
        self.require(record, "native_project.sha256", lambda v: isinstance(v, str) and HEX64.fullmatch(v),
                     "64 lower-case hex characters", rid, file)
        if self.require(record, "spectrum", lambda v: isinstance(v, dict), "an object", rid, file) is not None:
            self.check_string_list(record["spectrum"], "band_facets", "spectrum.band_facets", rid, file)
        if isinstance(record.get("image_format"), dict):
            self.check_string_list(record["image_format"], "format_facets", "image_format.format_facets", rid, file)
        self.require(record, "record_hash", lambda v: isinstance(v, str) and HEX64.fullmatch(v),
                     "64 lower-case hex characters", rid, file)
        self.require(record, "record_hash_method", lambda v: isinstance(v, str), "a string", rid, file)
        self.check_record_members(record, rid, file)
        self.check_numeric_values(record, "", rid, file)
        self.check_member_types(record, "", rid, file)
        entries = record.get("configurations")
        if isinstance(entries, list):
            for index, entry in enumerate(entries):
                if isinstance(entry, dict):
                    prefix = f"configurations[{index}]."
                    self.check_numeric_values(entry, prefix, rid, file)
                    self.check_member_types(entry, prefix, rid, file)
                    for section, member in (("spectrum", "band_facets"), ("image_format", "format_facets")):
                        if isinstance(entry.get(section), dict):
                            self.check_string_list(entry[section], member, f"{prefix}{section}.{member}", rid, file)
        self.check_source_members(record, rid, file)
        return valid_id if isinstance(valid_id, str) else None

    def optional(self, container: dict[str, Any], member: str, check, expectation: str, dotted: str,
                 rid: str | None, file: str) -> None:
        """An optional member: absent, null or accepted by `check`; else INVALID_FIELD."""
        value = container.get(member)
        if value is not None and not check(value):
            self.add(ERROR, "INVALID_FIELD", rid, file, dotted, f"{dotted} must be {expectation}",
                     expected=expectation, actual=value)

    def check_record_members(self, record: dict[str, Any], rid: str | None, file: str) -> None:
        """Types of the record-level members outside the sections."""
        native = record.get("native_project")
        if isinstance(native, dict):
            self.optional(native, "engine_version", _is_text, "a string or null", "native_project.engine_version",
                          rid, file)
        collection = record.get("collection")
        if isinstance(collection, dict):
            self.optional(collection, "license", _is_object, "an object or null", "collection.license", rid, file)
            if isinstance(collection.get("license"), dict):
                for member in ("copyright", "spdx"):
                    self.optional(collection["license"], member, _is_text, "a string or null",
                                  f"collection.license.{member}", rid, file)
        self.optional(record, "validation", _is_object, "an object or null", "validation", rid, file)
        if isinstance(record.get("validation"), dict):
            for member in ("paraxial", "field", "project_check"):
                self.optional(record["validation"], member, _is_text, "a string or null", f"validation.{member}",
                              rid, file)

    def check_member_types(self, container: dict[str, Any], prefix: str, rid: str | None, file: str) -> None:
        """Types of every section and fact-object member the builder indexes or the query layer
        iterates, in a record or in one `configurations[]` entry."""
        rules = self.vocab.fact_extraction
        skipped = set(rules.get("skip_members", [])) | set(rules.get("check_members", []))
        for section in (*OBJECT_SECTIONS, "analysis_evidence", "design"):
            if section in OBJECT_SECTIONS or section == "analysis_evidence":
                self.optional(container, section, _is_object, "an object or null", f"{prefix}{section}", rid, file)
            content = container.get(section)
            if not isinstance(content, dict) or (prefix and section == "design"):
                continue
            where = f"{prefix}{section}"
            if section != "analysis_evidence":
                self.check_fact_object(content, where, rid, file)
            for member, value in content.items():
                if member.endswith("_basis"):
                    self.optional(content, member, _is_text, "a string or null", f"{where}.{member}", rid, file)
                if member in skipped:
                    continue
                if section == "analysis_evidence":
                    self.optional(content, member, _is_object, "an object or null (a study)", f"{where}.{member}",
                                  rid, file)
                    if isinstance(value, dict):
                        self.optional(value, "status", _is_text, "a string or null", f"{where}.{member}.status",
                                      rid, file)
                if isinstance(value, dict):
                    self.check_fact_object(value, f"{where}.{member}", rid, file)
        performance = container.get("performance")
        if isinstance(performance, list):
            for index, entry in enumerate(performance):
                if isinstance(entry, dict):
                    self.check_fact_object(entry, f"{prefix}performance[{index}]", rid, file)

    def check_fact_object(self, obj: dict[str, Any], where: str, rid: str | None, file: str) -> None:
        for member in TEXT_MEMBERS:
            self.optional(obj, member, _is_text, "a string or null", f"{where}.{member}", rid, file)
        self.optional(obj, "conditions", lambda v: isinstance(v, (dict, str)), "an object, a string or null",
                      f"{where}.conditions", rid, file)
        self.optional(obj, "system_aperture", lambda v: isinstance(v, bool), "true, false or null",
                      f"{where}.system_aperture", rid, file)
        self.optional(obj, "sampled_field_fractions",
                      lambda v: isinstance(v, list) and bool(v) and all(is_number(item) for item in v),
                      "a non-empty list of finite numbers or null", f"{where}.sampled_field_fractions", rid, file)

    def check_source_members(self, record: dict[str, Any], rid: str | None, file: str) -> None:
        """A `source` anywhere in the fact data (every member outside `non_fact_sections`, and the
        `configurations[]` entries; any depth, lists included) is an evidence reference object or
        null. `collection` and `materials` are not fact data: their `source` members are prose."""
        non_fact = set(self.vocab.fact_extraction.get("non_fact_sections", []))
        facts = {key: value for key, value in record.items() if key not in non_fact}
        locations = list(non_object_sources(facts, ""))
        if isinstance(record.get("configurations"), list):
            locations.extend(non_object_sources(record["configurations"], "configurations"))
        for where in locations:
            self.evidence_problem(MALFORMED, rid, file, where,
                                  f"{where} must be an evidence reference object {{path, sha256, section}}")

    def check_string_list(self, container: dict[str, Any], member: str, dotted: str, rid: str | None, file: str) -> None:
        if member in container and not (isinstance(container[member], list)
                                         and all(isinstance(item, str) for item in container[member])):
            self.add(ERROR, "INVALID_FIELD", rid, file, dotted, f"{dotted} must be a list of strings")

    def check_numeric_values(self, record: dict[str, Any], prefix: str, rid: str | None, file: str) -> None:
        """Numeric fact values that are present must be finite numbers (not bool) or null."""
        rules = self.vocab.fact_extraction
        skip_sections = set(rules.get("non_fact_sections", [])) | {"performance", "analysis_evidence"}
        for section, content in record.items():
            if section in skip_sections or not isinstance(content, dict):
                continue
            for member, value in content.items():
                if not isinstance(value, dict):
                    continue
                for key, item in value.items():
                    numeric_key = key == "value" or (section == "spectrum" and key in ("min_nm", "max_nm"))
                    if numeric_key and item is not None and not is_number(item):
                        self.add(ERROR, "INVALID_FIELD", rid, file, f"{prefix}{section}.{member}.{key}",
                                 f"{section}.{member}.{key} must be a finite number or null")
        performance = record.get("performance")
        if "performance" in record and not isinstance(performance, list):
            self.add(ERROR, "INVALID_FIELD", rid, file, f"{prefix}performance", "performance must be a list")
            return
        for index, entry in enumerate(performance or []):
            where = f"{prefix}performance[{index}]"
            if not isinstance(entry, dict):
                self.add(ERROR, "INVALID_FIELD", rid, file, where, "a performance entry must be an object")
                continue
            if not isinstance(entry.get("metric"), str) or not entry.get("metric"):
                self.add(ERROR, "MISSING_FIELD" if "metric" not in entry else "INVALID_FIELD", rid, file,
                         f"{where}.metric", "a performance entry needs a non-empty string metric")
            for key, item in entry.items():
                if PERFORMANCE_NUMERIC.fullmatch(key) and not key.endswith("_basis") and item is not None \
                        and not is_number(item):
                    self.add(ERROR, "INVALID_FIELD", rid, file, f"{where}.{key}",
                             f"performance value {key} must be a finite number or null")

    # Project and evidence -----------------------------------------------------------------

    def load_project(self, record: dict[str, Any], rid: str, file: str) -> tuple[str | None, bytes | None, Any]:
        """Validate the native Project reference. Returns (normalized path, LF bytes, parsed)."""
        native = record.get("native_project")
        if not isinstance(native, dict) or not isinstance(native.get("path"), str):
            return None, None, None
        raw_path = native["path"]
        rel_path = corpus_relative_path(raw_path)
        if rel_path is None or self.file_state(rel_path).escapes:
            self.add(ERROR, "PROJECT_PATH_ESCAPES", rid, file, "native_project.path",
                     f"native_project.path {raw_path!r} must be a relative path inside the corpus")
            return None, None, None
        state = self.file_state(rel_path)
        if not state.exists:
            self.add(ERROR, "PROJECT_MISSING", rid, file, "native_project.path",
                     f"native Project {rel_path} does not exist")
            return None, None, None
        stated = native.get("sha256")
        if isinstance(stated, str) and HEX64.fullmatch(stated) and stated != state.sha256:
            self.add(ERROR, "PROJECT_HASH_MISMATCH", rid, file, "native_project.sha256",
                     f"SHA-256 of {rel_path} (LF-normalized) differs from native_project.sha256",
                     expected=state.sha256, actual=stated)
        try:
            project = loads_strict(state.content or b"", reject_duplicate_keys=True)
        except JsonFormatError as exc:
            self.add(ERROR, "PROJECT_MALFORMED", rid, file, "native_project.path",
                     f"native Project {rel_path} is not valid JSON: {exc.message}")
            return rel_path, state.content, None
        if not isinstance(project, dict) or project.get("schema") != PROJECT_SCHEMA:
            self.add(ERROR, "PROJECT_MALFORMED", rid, file, "native_project.path",
                     f"native Project {rel_path} must be a JSON object with schema {PROJECT_SCHEMA!r}")
            return rel_path, state.content, None
        return rel_path, state.content, project

    def evidence_problem(self, severity_status: str, rid: str, file: str, field_path: str, message: str,
                         **compare: Any) -> None:
        code = EVIDENCE_CODES[severity_status]
        self.add(WARNING if self.allow else ERROR, code, rid, file, field_path, message, **compare)

    def resolve_ref(self, ref_field: str, kind: str, obj: dict[str, Any], rid: str, file: str) -> EvidenceRef:
        """Resolve one evidence reference (amendment 10.4) and report problems."""
        raw_path, digest, section = obj.get("path"), obj.get("sha256"), obj.get("section")
        ref = EvidenceRef(ref_field, kind, raw_path if isinstance(raw_path, str) else None,
                          digest if isinstance(digest, str) else None,
                          section if isinstance(section, str) else None, MALFORMED)
        if isinstance(raw_path, str) and raw_path:
            rel_path = corpus_relative_path(raw_path)
            if rel_path is None or self.file_state(rel_path).escapes:
                ref.status = PATH_ESCAPES
                self.add(ERROR, "PROJECT_PATH_ESCAPES", rid, file, f"{ref_field}.path",
                         f"evidence path {raw_path!r} must be a relative path inside the corpus")
                return ref
        else:
            self.evidence_problem(MALFORMED, rid, file, f"{ref_field}.path", "an evidence path must be a non-empty string")
            return ref
        if not (isinstance(digest, str) and HEX64.fullmatch(digest)):
            self.evidence_problem(MALFORMED, rid, file, f"{ref_field}.sha256",
                                  "an evidence sha256 must be 64 lower-case hex characters")
            return ref
        if kind == "source" and not (isinstance(section, str) and section):
            self.evidence_problem(MALFORMED, rid, file, f"{ref_field}.section", "an evidence section must be a non-empty string")
            return ref
        state = self.file_state(rel_path)
        if not state.exists:
            ref.status = FILE_MISSING
            self.evidence_problem(FILE_MISSING, rid, file, f"{ref_field}.path", f"evidence file {rel_path} does not exist")
            return ref
        if state.sha256 != digest:
            ref.status = HASH_MISMATCH
            self.evidence_problem(HASH_MISMATCH, rid, file, f"{ref_field}.sha256",
                                  f"SHA-256 of {rel_path} (LF-normalized) differs from the reference",
                                  expected=state.sha256, actual=digest)
            return ref
        self.parsed_evidence(state)
        if state.parsed is None:
            ref.status = MALFORMED
            self.evidence_problem(MALFORMED, rid, file, f"{ref_field}.path",
                                  f"evidence file {rel_path} is malformed: {state.problem}")
            return ref
        if ref_field == "collection.evidence_extract":
            # Checked before the file is stored: the hash proves which bytes are pinned, only the
            # extract's record_id says they are this record's. Section sources may name any file.
            broken = evidence_owner_problem(state.parsed, rid)
            if broken is not None:
                ref.status = MALFORMED
                self.evidence_problem(MALFORMED, rid, file, f"{ref_field}.path",
                                      f"evidence file {rel_path} is malformed: {broken}")
                return ref
        self.evidence_files[rel_path] = (state.sha256, state.content or b"")
        if kind == "source" and state.parsed["sections"].get(section) is None:
            ref.status = SECTION_MISSING
            self.evidence_problem(SECTION_MISSING, rid, file, f"{ref_field}.section",
                                  f"evidence file {rel_path} has no section {section!r}")
            return ref
        ref.status = RESOLVED
        return ref

    def resolve_notice(self, record: dict[str, Any], rid: str, file: str) -> EvidenceRef | None:
        license_info = record.get("collection", {}).get("license") if isinstance(record.get("collection"), dict) else None
        if not isinstance(license_info, dict) or "notice_path" not in license_info:
            return None
        field_path = "collection.license.notice_path"
        raw_path = license_info["notice_path"]
        ref = EvidenceRef(field_path, "license_notice", raw_path if isinstance(raw_path, str) else None, None, None,
                          MALFORMED)
        severity = WARNING if self.allow else ERROR
        if not isinstance(raw_path, str) or not raw_path:
            # A null, empty or mistyped notice path is a missing notice, not a path escape.
            self.add(severity, "LICENSE_NOTICE_MISSING", rid, file, field_path,
                     f"license notice path must be a non-empty string, got {raw_path!r}")
            return ref
        rel_path = corpus_relative_path(raw_path)
        if rel_path is None or self.file_state(rel_path).escapes:
            ref.status = PATH_ESCAPES
            self.add(ERROR, "PROJECT_PATH_ESCAPES", rid, file, field_path,
                     f"license notice path {raw_path!r} must be a relative path inside the corpus")
            return ref
        state = self.file_state(rel_path)
        text = None
        if state.exists:
            try:
                text = (state.content or b"").decode("utf-8")
            except UnicodeDecodeError:
                text = None
        if text is None:
            ref.status = FILE_MISSING
            self.add(severity, "LICENSE_NOTICE_MISSING", rid, file, field_path,
                     f"license notice {rel_path} does not exist or is not UTF-8 text")
            return ref
        self.licenses[rel_path] = text
        copyright_line = license_info.get("copyright")
        if isinstance(copyright_line, str) and copyright_line not in text:
            ref.status = NOTICE_MISMATCH
            self.add(severity, "LICENSE_NOTICE_MISMATCH", rid, file, "collection.license.copyright",
                     f"license notice {rel_path} does not contain the stated copyright line")
            return ref
        ref.status = RESOLVED
        return ref

    def collect_refs(self, record: dict[str, Any], rid: str, file: str) -> list[EvidenceRef]:
        refs: list[EvidenceRef] = []
        for ref_field, obj in iter_source_refs(record):
            refs.append(self.resolve_ref(ref_field, "source", obj, rid, file))
        collection = record.get("collection") if isinstance(record.get("collection"), dict) else {}
        extract = collection.get("evidence_extract")
        if "evidence_extract" in collection and not isinstance(extract, dict):
            self.evidence_problem(MALFORMED, rid, file, "collection.evidence_extract",
                                  "collection.evidence_extract must be an object {path, sha256}")
        if isinstance(extract, dict) and not {"path", "sha256", "section"} <= extract.keys():
            refs.append(self.resolve_ref("collection.evidence_extract", "evidence_extract", extract, rid, file))
        notice = self.resolve_notice(record, rid, file)
        if notice is not None:
            refs.append(notice)
        refs.sort(key=lambda ref: ref.field)
        return refs

    # Licenses -----------------------------------------------------------------------------

    def load_licenses(self) -> None:
        candidates = [self.root / "LICENSE"]
        licenses_dir = self.root / "licenses"
        if licenses_dir.is_dir():
            candidates.extend(sorted(p for p in licenses_dir.glob("*.txt") if p.is_file()))
        for path in candidates:
            if not path.is_file():
                continue
            rel_path = self.rel(path)
            try:
                self.licenses[rel_path] = lf(path.read_bytes()).decode("utf-8")
            except UnicodeDecodeError:
                self.add(ERROR, "LICENSE_UNREADABLE", None, rel_path, "", f"{rel_path} is not UTF-8 text")

    # Whole corpus -------------------------------------------------------------------------

    def run(self) -> Corpus:
        if not (self.root / "records").is_dir():
            raise LensError(
                "INVALID_REQUEST",
                f"corpus root {self.root} has no records/ directory",
                hint="Pass --corpus with the directory that contains records/ (the repository root).",
                details={"corpus": str(self.root)},
            )
        record_files, manifests = self.discover()
        loaded: list[tuple[str, dict[str, Any], str | None]] = []
        for path in record_files:
            file = self.rel(path)
            data = self.load_json(path)
            if data is _UNREADABLE:
                continue
            if not isinstance(data, dict):
                self.add(ERROR, "MALFORMED_JSON", None, file, "",
                         f"a record must be a JSON object, not {_json_type(data)}")
                continue
            loaded.append((file, data, self.validate_record(data, file)))

        by_id: dict[str, list[str]] = {}
        for file, data, rid in loaded:
            if rid is not None:
                by_id.setdefault(rid, []).append(file)
        for rid, files in by_id.items():
            if len(files) > 1:
                for file in files:
                    others = ", ".join(other for other in files if other != file)
                    self.add(ERROR, "DUPLICATE_RECORD_ID", rid, file, "record_id",
                             f"record_id {rid!r} is also used by {others}; neither is indexed")

        normalized: list[NormalizedRecord] = []
        for file, data, rid in loaded:
            if rid is None:
                continue
            item = self.check_record(file, data, rid)
            failed = any(f.severity == ERROR and (f.file == file or f.record_id == rid) for f in self.findings)
            if item is not None and not failed:
                normalized.append(item)

        self.check_manifests(manifests, loaded)
        self.load_licenses()
        normalized.sort(key=lambda rec: rec.record_id)
        evidence = dict(sorted(self.evidence_files.items()))
        return Corpus(self.root, normalized, evidence, dict(sorted(self.licenses.items())),
                      sort_findings(self.findings))

    def analysis_document(self, reference: Any) -> dict[str, Any] | None:
        if not isinstance(reference, dict):
            return None
        path = corpus_relative_path(reference.get("path"))
        if path is None:
            return None
        state = self.file_state(path)
        if state.sha256 != reference.get("sha256") or state.content is None:
            return None
        self.parsed_evidence(state)
        return state.parsed if isinstance(state.parsed, dict) and state.problem is None else None

    def check_record(self, file: str, record: dict[str, Any], rid: str) -> NormalizedRecord | None:
        """Identity, hash, Project, configuration, evidence and normalization checks for one record."""
        parts = file.split("/")
        collection_dir, record_dir = parts[1], parts[2]
        if record_dir != rid:
            self.add(ERROR, "RECORD_DIR_MISMATCH", rid, file, "record_id",
                     f"record directory {record_dir!r} differs from record_id", expected=rid, actual=record_dir)
        collection = record.get("collection") if isinstance(record.get("collection"), dict) else {}
        collection_id = collection.get("collection_id")
        if isinstance(collection_id, str) and collection_id != collection_dir:
            self.add(WARNING, "COLLECTION_DIR_MISMATCH", rid, file, "collection.collection_id",
                     f"collection directory {collection_dir!r} differs from collection_id",
                     expected=collection_dir, actual=collection_id)
        stated_hash = record.get("record_hash")
        if isinstance(stated_hash, str) and HEX64.fullmatch(stated_hash):
            computed = record_hash(record)
            if computed != stated_hash:
                self.add(ERROR, "RECORD_HASH_MISMATCH", rid, file, "record_hash",
                         "record_hash differs from the hash recomputed from the parsed record",
                         expected=computed, actual=stated_hash)

        project_path, project_bytes, project = self.load_project(record, rid, file)
        natives, project_issues = native_configurations(project)
        issues = list(project_issues) + check_configurations(record, natives)
        refs = self.collect_refs(record, rid, file)

        views = configuration_views(record, project)
        design = record.get("design") if isinstance(record.get("design"), dict) else {}
        count = design.get("configuration_count")
        if isinstance(count, int) and not isinstance(count, bool) and count != len(views):
            issues.append(Issue(ERROR, "CONFIGURATION_COUNT_MISMATCH", "design.configuration_count",
                                f"design.configuration_count is {count} but the record indexes {len(views)} "
                                "configuration(s)"))
        config_facets: dict[str, list[FacetValue]] = {}
        facts: dict[str, list[dict[str, Any]]] = {}
        project_digest = sha256_hex(project_bytes) if project_bytes is not None else None
        revision = record.get("record_revision")
        extract = self.analysis_document(collection.get("evidence_extract"))
        for view in views:
            def applies(reference: dict[str, Any]) -> bool:
                document = self.analysis_document(reference)
                return document is None or analysis_applies(document, project_digest, view.configuration_id, revision)

            camera_evidence = (extract is not None and extract.get("native_project_sha256") == project_digest
                               and extract.get("record_id") == rid
                               and analysis_applies(extract, project_digest, view.configuration_id, revision))
            declares_aperture = bool(system_aperture_names(view.view))
            view.view = project_view(view.view, project, as_written=not natives,
                                     applies=applies, camera_evidence=camera_evidence)
            values, view_issues = configuration_facets(view.view, self.vocab)
            config_facets[view.configuration_key] = values
            issues.extend(view_issues)
            facts[view.configuration_key] = extract_facts(view.view, self.vocab)
            if not declares_aperture and not system_aperture_names(view.view):
                issues.append(Issue(WARNING, "NO_SYSTEM_APERTURE", "aperture",
                                    "no aperture fact is marked system_aperture: true"
                                    + (f" in configuration {view.configuration_id}" if view.configuration_id else "")))
            for fact in facts[view.configuration_key]:
                if fact["value_num"] is not None and fact["unit"] is None and not fact["fact_key"].startswith("design."):
                    issues.append(Issue(WARNING, "UNIT_UNKNOWN", fact["fact_key"],
                                        f"no unit can be inferred for numeric fact {fact['fact_key']}"))
        facets, facet_issues = record_facets(views[0].view if views else record, self.vocab)
        for issue in (*issues, *facet_issues):
            issue_file = project_path if issue.in_project and project_path else file
            self.add(issue.severity, issue.code, rid, issue_file, issue.field, issue.message)

        if project_path is None or project_bytes is None or project is None:
            return None
        native = record["native_project"]
        title = record.get("title")
        if isinstance(collection_id, str) and collection_id in PHONE_COLLECTIONS:
            # These intake titles embed printed f-numbers and field angles.
            role = design.get("module_role")
            title = f"{role.replace('_', ' ').capitalize()} phone lens" if isinstance(role, str) else "Phone lens"
        return NormalizedRecord(
            file=file,
            record=record,
            record_id=rid,
            revision=record.get("record_revision"),
            schema_version=record.get("record_schema_version"),
            title=title,
            collection_id=collection_id,
            family_id=design.get("design_family_id") if isinstance(design.get("design_family_id"), str) else None,
            design_scope=design.get("design_scope") if isinstance(design.get("design_scope"), str) else None,
            record_hash=stated_hash,
            project_path=project_path,
            project_sha256=sha256_hex(project_bytes),
            project_bytes=project_bytes,
            engine_version=native.get("engine_version") if isinstance(native.get("engine_version"), str) else None,
            natives=natives,
            views=views,
            record_facets=facets,
            config_facets=config_facets,
            facts=facts,
            evidence_refs=refs,
        )

    def check_manifests(self, manifests: dict[str, list[Path]],
                        loaded: list[tuple[str, dict[str, Any], str | None]]) -> None:
        """Batch manifest cross-checks (amendment 10.21)."""
        present: dict[str, set[str]] = {}
        for _, data, rid in loaded:
            collection = data.get("collection") if isinstance(data.get("collection"), dict) else {}
            if rid is not None and isinstance(collection.get("collection_id"), str):
                present.setdefault(collection["collection_id"], set()).add(rid)
        listed: dict[str, set[str]] = {}
        for collection_dir, paths in sorted(manifests.items()):
            for path in paths:
                file = self.rel(path)
                data = self.load_json(path)
                if data is _UNREADABLE:
                    continue
                if not isinstance(data, dict):
                    self.add(ERROR, "MALFORMED_JSON", None, file, "",
                             f"a batch manifest must be a JSON object, not {_json_type(data)}")
                    continue
                if self.require(data, "collection", _is_object, "an object", None, file) is None:
                    continue
                collection_id = self.require(data, "collection.collection_id",
                                             lambda v: isinstance(v, str) and v, "a non-empty string", None, file)
                if collection_id is None:
                    continue
                if collection_id != collection_dir:
                    self.add(WARNING, "BATCH_MANIFEST_MISMATCH", None, file, "collection.collection_id",
                             f"manifest collection {collection_id!r} differs from its directory {collection_dir!r}",
                             expected=collection_dir, actual=collection_id)
                accepted = data.get("accepted_record_ids")
                if not isinstance(accepted, list) or not all(isinstance(item, str) for item in accepted):
                    self.add(ERROR, "INVALID_FIELD", None, file, "accepted_record_ids",
                             "accepted_record_ids must be a list of record ids")
                    continue
                ids = listed.setdefault(collection_id, set())
                for index, rid in enumerate(accepted):
                    ids.add(rid)
                    if rid not in present.get(collection_id, set()):
                        self.add(ERROR, "BATCH_MANIFEST_MISMATCH", rid, file, f"accepted_record_ids[{index}]",
                                 f"accepted record {rid!r} is not present in collection {collection_id!r}")
        for file, data, rid in loaded:
            collection = data.get("collection") if isinstance(data.get("collection"), dict) else {}
            collection_id = collection.get("collection_id")
            if rid is None or not isinstance(collection_id, str) or collection_id not in listed:
                continue
            if rid not in listed[collection_id]:
                self.add(WARNING, "BATCH_MANIFEST_MISMATCH", rid, file, "record_id",
                         f"record {rid!r} is not listed in any batch manifest of collection {collection_id!r}")


def load_corpus(root: Path | str, *, allow_unresolved_evidence: bool = False,
                vocabulary: Vocabulary | None = None) -> Corpus:
    """Discover, validate and normalize the corpus under `root` (the directory containing records/).

    Corpus problems are returned as findings (sorted by file, field, code); only a missing records/
    directory raises (`INVALID_REQUEST`). With `allow_unresolved_evidence`, broken evidence and license
    notice references become warnings and are stored with their failure status.
    """
    return _Loader(Path(root), allow_unresolved_evidence, vocabulary or load_vocabulary()).run()

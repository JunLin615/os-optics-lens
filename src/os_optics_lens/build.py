"""Index builder: writes the SQLite index atomically (spec §5, amendments 10.17-10.19).

`build_index` validates and normalizes the corpus, then writes `<out>.tmp-<pid>` in the output
directory, checks integrity, VACUUMs, closes it and `os.replace`s it onto `<out>`. Any corpus error
fails the build with `BUILD_FAILED` before a file is written; any failure while writing removes the
temporary file. A previous index at `<out>` is left byte-identical unless the build succeeds.

Reproducibility: rows are inserted in sorted order, all stored JSON is canonical and nothing
time-, path- or environment-dependent is stored, so two builds of the same inputs with the same
Python/SQLite are byte-identical and `dump_index` is identical across environments.

meta also records what a reader must agree with: `vocabulary_digest` and `index_semantics` (the
fingerprint of the index-producing sources, see `semantics`); `store.Index` refuses an index whose
values differ from its own.

snapshot_id (10.19) = first 16 hex of SHA-256 over the canonical JSON of {index_schema_version,
builder_version, vocabulary_digest, allow_unresolved_evidence, content_digest}, where content_digest is
the SHA-256 of the canonical dump of every table except meta.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__
from .canonical import canonical_json, sha256_hex, sha256_text
from .corpus import Corpus, load_corpus
from .errors import LensError
from .semantics import index_fingerprint
from .vocabulary import Vocabulary, load_vocabulary

INDEX_SCHEMA_VERSION = "2"
BUILDER = f"os-optics-lens {__version__}"

SCHEMA_SQL = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE records(
  record_pk INTEGER PRIMARY KEY,
  record_id TEXT UNIQUE NOT NULL,
  revision INTEGER NOT NULL,
  record_schema_version TEXT,
  title TEXT,
  collection_id TEXT,
  family_id TEXT,
  design_scope TEXT,
  record_hash TEXT,
  record_path TEXT,
  project_path TEXT,
  project_sha256 TEXT,
  engine_version TEXT,
  configuration_count INTEGER,
  record_json TEXT NOT NULL
);
CREATE TABLE configurations(
  config_pk INTEGER PRIMARY KEY,
  record_pk INTEGER NOT NULL,
  configuration_key TEXT NOT NULL,
  native INTEGER NOT NULL,
  label TEXT,
  ordinal INTEGER NOT NULL,
  view_json TEXT NOT NULL,
  UNIQUE(record_pk, configuration_key)
);
CREATE TABLE facets(
  record_pk INTEGER NOT NULL,
  config_pk INTEGER,
  facet TEXT NOT NULL,
  value TEXT NOT NULL,
  basis TEXT NOT NULL
);
CREATE INDEX facets_facet_value ON facets(facet, value);
CREATE INDEX facets_record ON facets(record_pk, config_pk);
CREATE TABLE facts(
  fact_pk INTEGER PRIMARY KEY,
  record_pk INTEGER NOT NULL,
  config_pk INTEGER NOT NULL,
  fact_key TEXT NOT NULL,
  json_path TEXT NOT NULL,
  metric TEXT,
  value_num REAL,
  value_min REAL,
  value_max REAL,
  value_json TEXT,
  unit TEXT,
  basis TEXT,
  basis_class TEXT,
  conditions_json TEXT,
  conditions_text TEXT,
  definition TEXT,
  eligible_for TEXT,
  state TEXT,
  system_aperture INTEGER,
  qualifiers_json TEXT,
  value_paths TEXT,
  constraint_form TEXT,
  sampled_field_fractions_json TEXT,
  source_ref_pk INTEGER
);
CREATE INDEX facts_key_value ON facts(fact_key, value_num);
CREATE INDEX facts_config ON facts(config_pk);
CREATE TABLE evidence_files(path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, content BLOB NOT NULL);
CREATE TABLE evidence_refs(
  ref_pk INTEGER PRIMARY KEY,
  record_pk INTEGER NOT NULL,
  field TEXT NOT NULL,
  path TEXT,
  sha256 TEXT,
  section TEXT,
  status TEXT NOT NULL
);
CREATE INDEX evidence_refs_record ON evidence_refs(record_pk);
CREATE TABLE projects(
  record_pk INTEGER PRIMARY KEY,
  sha256 TEXT NOT NULL,
  content BLOB NOT NULL,
  native_configuration_ids TEXT
);
CREATE TABLE licenses(path TEXT PRIMARY KEY, content TEXT NOT NULL);
CREATE TABLE findings(severity TEXT NOT NULL, code TEXT NOT NULL, record_id TEXT, file TEXT NOT NULL,
  field TEXT NOT NULL, message TEXT NOT NULL);
"""

FACT_COLUMNS = (
    "fact_key", "json_path", "metric", "value_num", "value_min", "value_max", "value_json", "unit", "basis",
    "basis_class", "conditions_json", "conditions_text", "definition", "eligible_for", "state",
    "system_aperture", "qualifiers_json", "value_paths", "constraint_form", "sampled_field_fractions_json",
)
TABLES = ("meta", "records", "configurations", "facets", "facts", "evidence_files", "evidence_refs",
          "projects", "licenses", "findings")
"""Every table of the index schema."""


@dataclass
class BuildResult:
    """Outcome of a successful build or validation.

    `out` is the absolute index path (None for `validate_corpus`); `warnings` is the number of warning
    findings and `findings` lists them (dicts, sorted by file, field, code).
    """

    out: Path | None
    snapshot_id: str
    records: int
    configurations: int
    warnings: int
    findings: list[dict[str, Any]] = field(default_factory=list)
    facts: int = 0

    def to_dict(self) -> dict[str, Any]:
        """JSON form used by `index build`/`index validate` output (`use` names the --index flag)."""
        return {
            "out": str(self.out) if self.out is not None else None,
            "snapshot_id": self.snapshot_id,
            "records": self.records,
            "configurations": self.configurations,
            "facts": self.facts,
            "warnings": self.warnings,
            "findings": self.findings,
            "use": f"--index {self.out}" if self.out is not None else None,
        }


def _remove(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


def _corpus_digest(corpus: Corpus) -> str:
    """SHA-256 over [record_id, revision, record_hash, project_sha256, sorted (path, sha256) evidence]."""
    rows = []
    for record in corpus.records:
        evidence = sorted({(ref.path or "", ref.sha256 or "") for ref in record.evidence_refs if ref.sha256})
        rows.append([record.record_id, record.revision, record.record_hash, record.project_sha256,
                     [list(item) for item in evidence]])
    return sha256_text(canonical_json(sorted(rows)))


def _record_metadata(record: dict[str, Any]) -> dict[str, Any]:
    """Identity and direct provenance for inspection, separate from the Project fact view."""
    result = {key: record[key] for key in ("record_id", "record_revision", "record_schema_version", "record_hash")
              if key in record}
    result["native_project"] = {key: record["native_project"][key] for key in ("path", "sha256", "engine_version")
                                if key in record["native_project"]}
    collection = record["collection"]
    result["collection"] = {key: collection[key] for key in (
        "collection_id", "source", "source_url", "source_input", "source_revision", "source_input_sha256",
        "source_links", "contributor", "attribution", "original_publication", "redistribution", "license",
    ) if key in collection}
    return result


def _insert(conn: sqlite3.Connection, corpus: Corpus) -> tuple[int, int]:
    """Insert every non-meta table in sorted order. Returns (configuration count, fact count)."""
    config_pk = 0
    fact_pk = 0
    ref_pk = 0
    for record_pk, record in enumerate(corpus.records, start=1):
        conn.execute(
            "INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (record_pk, record.record_id, record.revision, record.schema_version, record.title,
             record.collection_id, record.family_id, record.design_scope, record.record_hash, record.file,
             record.project_path, record.project_sha256, record.engine_version, len(record.views),
             canonical_json(_record_metadata(record.record))),
        )
        conn.execute(
            "INSERT INTO projects VALUES (?,?,?,?)",
            (record_pk, record.project_sha256, record.project_bytes,
             canonical_json([native.configuration_id for native in record.natives])),
        )
        ref_by_triple: dict[tuple[Any, Any, Any], int] = {}
        ref_by_field: dict[str, int] = {}
        for ref in record.evidence_refs:
            ref_pk += 1
            conn.execute("INSERT INTO evidence_refs VALUES (?,?,?,?,?,?,?)",
                         (ref_pk, record_pk, ref.field, ref.path, ref.sha256, ref.section, ref.status))
            if ref.kind == "source":
                ref_by_triple.setdefault((ref.path, ref.sha256, ref.section), ref_pk)
                ref_by_field[ref.field] = ref_pk
        entries = record.record.get("configurations")
        for value in record.record_facets:
            conn.execute("INSERT INTO facets VALUES (?,?,?,?,?)",
                         (record_pk, None, value.facet, value.value, value.basis))
        for view in record.views:
            config_pk += 1
            conn.execute(
                "INSERT INTO configurations VALUES (?,?,?,?,?,?,?)",
                (config_pk, record_pk, view.configuration_key, int(view.native), view.label, view.ordinal,
                 canonical_json(view.view)),
            )
            for value in record.config_facets.get(view.configuration_key, []):
                conn.execute("INSERT INTO facets VALUES (?,?,?,?,?)",
                             (record_pk, config_pk, value.facet, value.value, value.basis))
            entry = entries[view.ordinal] if isinstance(entries, list) and view.native and view.ordinal < len(entries)                 else None
            for fact in record.facts.get(view.configuration_key, []):
                fact_pk += 1
                source_ref = _source_ref(fact, entry, view.ordinal, ref_by_field, ref_by_triple)
                values = [fact[column] for column in FACT_COLUMNS]
                values[FACT_COLUMNS.index("system_aperture")] = (
                    None if fact["system_aperture"] is None else int(fact["system_aperture"]))
                conn.execute(
                    f"INSERT INTO facts VALUES ({','.join('?' * (len(FACT_COLUMNS) + 4))})",
                    (fact_pk, record_pk, config_pk, *values, source_ref),
                )
    for path, (digest, content) in corpus.evidence_files.items():
        conn.execute("INSERT INTO evidence_files VALUES (?,?,?)", (path, digest, content))
    for path, content in corpus.licenses.items():
        conn.execute("INSERT INTO licenses VALUES (?,?)", (path, content))
    for item in corpus.warnings:
        conn.execute("INSERT INTO findings VALUES (?,?,?,?,?,?)",
                     (item.severity, item.code, item.record_id, item.file, item.field, item.message))
    return config_pk, fact_pk


def _source_ref(fact: dict[str, Any], entry: Any, ordinal: int, by_field: dict[str, int],
                by_triple: dict[tuple[Any, Any, Any], int]) -> int | None:
    """The evidence_refs row of a fact's own source: the ref at the same record location when it
    exists (inside `configurations[ordinal]` when the section comes from that entry), else the first
    ref with the same (path, sha256, section)."""
    source = fact.get("source")
    if not isinstance(source, dict):
        return None
    location = fact.get("source_field")
    if isinstance(location, str):
        section = location.split(".", 1)[0].split("[", 1)[0]
        if isinstance(entry, dict) and section in entry:
            location = f"configurations[{ordinal}].{location}"
        if location in by_field:
            return by_field[location]
    # A reference row keeps only string members (a mistyped one is None), so only strings can key.
    triple = tuple(source.get(key) for key in ("path", "sha256", "section"))
    return by_triple.get(tuple(value if isinstance(value, str) else None for value in triple))


def _dump_tables(conn: sqlite3.Connection, tables: tuple[str, ...]) -> dict[str, Any]:
    """Canonical dump of `tables`: columns and rows ordered by every column; blobs as sha256+length."""
    out: dict[str, Any] = {}
    for table in tables:
        cursor = conn.execute(f"SELECT * FROM {table} LIMIT 0")
        columns = [description[0] for description in cursor.description]
        order = ",".join(str(i) for i in range(1, len(columns) + 1))
        rows = []
        for row in conn.execute(f"SELECT * FROM {table} ORDER BY {order}"):
            rows.append([
                {"sha256": sha256_hex(bytes(value)), "length": len(value)} if isinstance(value, (bytes, memoryview))
                else value
                for value in row
            ])
        out[table] = {"columns": columns, "rows": rows}
    return out


def _write_index(path: Path, corpus: Corpus, allow_unresolved_evidence: bool, vocabulary: Vocabulary) -> BuildResult:
    with contextlib.closing(sqlite3.connect(str(path), isolation_level=None)) as conn:
        conn.execute("PRAGMA page_size=4096")
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("BEGIN")
        for statement in SCHEMA_SQL.split(";"):
            if statement.strip():
                conn.execute(statement)
        configurations, facts = _insert(conn, corpus)
        content_digest = sha256_text(canonical_json(_dump_tables(conn, TABLES[1:])))
        allow_flag = "1" if allow_unresolved_evidence else "0"
        snapshot_id = sha256_text(canonical_json({
            "index_schema_version": INDEX_SCHEMA_VERSION,
            "builder_version": __version__,
            "vocabulary_digest": vocabulary.digest,
            "allow_unresolved_evidence": allow_flag,
            "content_digest": content_digest,
        }))[:16]
        meta = {
            "index_schema_version": INDEX_SCHEMA_VERSION,
            "builder": BUILDER,
            "builder_version": __version__,
            "snapshot_id": snapshot_id,
            "record_count": str(len(corpus.records)),
            "configuration_count": str(configurations),
            "fact_count": str(facts),
            "corpus_digest": _corpus_digest(corpus),
            "content_digest": content_digest,
            "allow_unresolved_evidence": allow_flag,
            "vocabulary_digest": vocabulary.digest,
            "index_semantics": index_fingerprint(),
        }
        for key in sorted(meta):
            conn.execute("INSERT INTO meta VALUES (?,?)", (key, meta[key]))
        conn.execute("COMMIT")
        problems = [row[0] for row in conn.execute("PRAGMA integrity_check")]
        if problems != ["ok"]:
            raise LensError("INTERNAL", "the new index failed its integrity check", details={"problems": problems})
        conn.execute("VACUUM")
    warnings = [item.to_dict() for item in corpus.warnings]
    return BuildResult(None, snapshot_id, len(corpus.records), configurations, len(warnings), warnings, facts)


def _fail(corpus: Corpus) -> LensError:
    findings = [item.to_dict() for item in corpus.findings]
    errors = len(corpus.errors)
    evidence_related = any(item["code"].startswith(("EVIDENCE_", "LICENSE_NOTICE_")) and item["severity"] == "error"
                           for item in findings)
    hint = "Fix the findings listed in details.findings (each names the file and field)."
    if evidence_related:
        hint += " To index records whose evidence references do not resolve, rebuild with " \
                "--allow-unresolved-evidence; those references then leave constraints UNCONFIRMED."
    return LensError(
        "BUILD_FAILED",
        f"index build failed with {errors} error(s) and {len(findings) - errors} warning(s)",
        hint=hint,
        details={"errors": errors, "warnings": len(findings) - errors, "findings": findings},
    )


def build_index(corpus_root: Path | str, out: Path | str, *, allow_unresolved_evidence: bool = False) -> BuildResult:
    """Build the index for the corpus at `corpus_root` and atomically replace `out`.

    Raises `BUILD_FAILED` (details.findings) when the corpus has an error finding, `INDEX_IN_USE` when
    `out` cannot be replaced because it is open elsewhere, `INVALID_REQUEST` when `corpus_root` has no
    records/ directory or `out` is a directory or a read-only file. In every failure case nothing is
    written and a previous `out` is unchanged.
    """
    target = Path(out).resolve()
    if target.is_dir():
        raise LensError("INVALID_REQUEST", f"--out names a directory: {target}",
                        hint="Pass the index file path to --out, e.g. <dir>/index.sqlite.",
                        details={"out": str(target)})
    vocabulary = load_vocabulary()
    corpus = load_corpus(corpus_root, allow_unresolved_evidence=allow_unresolved_evidence, vocabulary=vocabulary)
    if corpus.errors:
        raise _fail(corpus)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f"{target.name}.tmp-{os.getpid()}")
    _remove(temp)
    try:
        result = _write_index(temp, corpus, allow_unresolved_evidence, vocabulary)
        try:
            os.replace(temp, target)
        except PermissionError as exc:
            if target.is_dir() or (target.exists() and not os.access(target, os.W_OK)):
                raise LensError(
                    "INVALID_REQUEST", f"cannot replace {target}: it is a directory or a read-only file",
                    hint="Make the file writable or choose another --out, then build again.",
                    details={"out": str(target), "os_error": exc.strerror},
                ) from None
            raise LensError(
                "INDEX_IN_USE",
                f"cannot replace {target}: it is open in another process",
                hint="Close programs using the index (or write to a different --out) and build again.",
                details={"out": str(target), "os_error": exc.strerror},
            ) from None
    except BaseException:
        _remove(temp)
        raise
    result.out = target
    return result


def validate_corpus(corpus_root: Path | str, *, allow_unresolved_evidence: bool = False) -> BuildResult:
    """Run the full build into a temporary file and discard it.

    Returns the `BuildResult` the build would produce (with `out` None); raises like `build_index`.
    """
    with tempfile.TemporaryDirectory(prefix="os-optics-lens-validate-") as scratch:
        result = build_index(corpus_root, Path(scratch) / "index.sqlite",
                             allow_unresolved_evidence=allow_unresolved_evidence)
    result.out = None
    return result


def dump_index(path: Path | str) -> dict[str, Any]:
    """Canonical dump of every table of an index (blobs as {sha256, length}).

    Identical across environments for the same corpus and builder version; used by
    `os-optics-lens index dump` and reproducibility checks. Raises `INDEX_UNAVAILABLE` if the file
    cannot be opened as an index.
    """
    from .store import Index

    with Index.open(path) as index:
        return {"index_schema_version": INDEX_SCHEMA_VERSION, "tables": _dump_tables(index.connection, TABLES)}

"""Read-only access to a built index (spec §5, amendments 10.4, 10.17, 10.18).

`Index.open(path=None)` resolves the index file in this order and uses the first one specified:
the `path` argument (`--index`), the `OS_OPTICS_LENS_INDEX` environment variable, then the bundled
`os_optics_lens/_data/index.sqlite`. There is no implicit lookup in the working directory. The file is
opened as `file:<abs>?mode=ro&immutable=1`, so read-only installs and read-only trees work and no
sidecar files are created. A missing, corrupt or schema-mismatched index, or one built with other
vocabulary or indexing code than this reader's (meta `vocabulary_digest` and `index_semantics`),
raises `INDEX_UNAVAILABLE` with `details.tried` and a hint naming `os-optics-lens index build`.

Evidence resolution is a pure function of the source object and the stored `evidence_files`
(`resolve_source_in`); it never reads the filesystem. Evidence bytes are verified before every
use: they must hash to the stored digest column AND to the digest the reference expects
(SQLite has no page checksums), else the status is HASH_MISMATCH. Caches hold verified content only.
"""

from __future__ import annotations

import contextlib
import difflib
import json
import os
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from importlib import resources
from pathlib import Path
from typing import Any, Union

from .canonical import HEX64, JsonFormatError, corpus_relative_path, loads_strict, sha256_hex
from .errors import LensError
from .semantics import index_fingerprint
from .vocabulary import load_vocabulary

ENV_VAR = "OS_OPTICS_LENS_INDEX"
INDEX_SCHEMA_VERSION = "2"
REQUIRED_TABLES = frozenset({"meta", "records", "configurations", "facets", "facts", "evidence_files",
                             "evidence_refs", "projects", "licenses", "findings"})
BUILD_HINT = ("Build an index with `os-optics-lens index build --corpus <dir> --out <file>`, then pass "
              f"`--index <file>` or set {ENV_VAR}=<file>.")
NOTICE_FIELD = "collection.license.notice_path"

FilesLookup = Union[Mapping[str, tuple[str, bytes]], Callable[[str], Union[tuple[str, bytes], None]]]
"""Evidence files by normalized corpus-relative path: path -> (stored sha256, LF bytes), or a
callable. The bytes are verified on every use."""


def _hashes_to(content: Any, digest: Any) -> bool:
    """True when `content` is bytes whose SHA-256 equals `digest`."""
    return isinstance(content, bytes) and sha256_hex(content) == digest


def resolve_source_in(files: FilesLookup, source: Any,
                      cache: dict[tuple[str, str], Any] | None = None) -> dict[str, Any]:
    """Resolve a fact's `source` object against stored evidence files (amendment 10.4).

    Returns `{"status": ...}` with status one of RESOLVED, MISSING (no source object), MALFORMED
    (path/section not non-empty strings or sha256 not 64 hex), PATH_ESCAPES, FILE_MISSING (no
    stored file at that path), HASH_MISMATCH (the file's bytes, hashed again on every call, do not
    match both its stored sha256 and the reference's) or SECTION_MISSING (`sections[section]`
    absent or null). RESOLVED results also carry `path`, `section` and `section_value` (the parsed
    section). `cache` memoizes parsed files by (path, sha256) and is filled from verified bytes only.
    """
    if not isinstance(source, dict):
        return {"status": "MISSING"}
    path, digest, section = source.get("path"), source.get("sha256"), source.get("section")
    if not (isinstance(path, str) and path and isinstance(section, str) and section
            and isinstance(digest, str) and HEX64.fullmatch(digest)):
        return {"status": "MALFORMED"}
    rel_path = corpus_relative_path(path)
    if rel_path is None:
        return {"status": "PATH_ESCAPES"}
    found = files(rel_path) if callable(files) else files.get(rel_path)
    if found is None:
        return {"status": "FILE_MISSING", "path": rel_path, "section": section}
    stored_digest, content = found
    if stored_digest != digest or not _hashes_to(content, digest):
        return {"status": "HASH_MISMATCH", "path": rel_path, "section": section}
    key = (rel_path, digest)
    parsed = cache.get(key) if cache is not None else None
    if parsed is None:
        try:
            parsed = loads_strict(content)
        except JsonFormatError:
            parsed = False
        if cache is not None:
            cache[key] = parsed
    sections = parsed.get("sections") if isinstance(parsed, dict) else None
    if not isinstance(sections, dict):
        return {"status": "MALFORMED", "path": rel_path, "section": section}
    value = sections.get(section)
    if value is None:
        return {"status": "SECTION_MISSING", "path": rel_path, "section": section}
    return {"status": "RESOLVED", "path": rel_path, "section": section, "section_value": value}


def _unavailable(message: str, tried: list[dict[str, Any]]) -> LensError:
    return LensError("INDEX_UNAVAILABLE", message, hint=BUILD_HINT, details={"tried": tried})


def _semantics_mismatch(conn: sqlite3.Connection) -> str | None:
    """Why this reader cannot use the index: it was built with another vocabulary or with other
    index-producing code, so its facts and facets would not mean what the reader assumes."""
    expected = {"vocabulary_digest": load_vocabulary().digest, "index_semantics": index_fingerprint()}
    for key, mine in expected.items():
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        stored = "none" if row is None else str(row[0])[:16]
        if row is None or row[0] != mine:
            return (f"reader mismatch: {key} of the index is {stored}, this reader has {mine[:16]}; "
                    "the index was built by other vocabulary or indexing code")
    return None


def _connect(path: Path) -> tuple[sqlite3.Connection | None, str | None]:
    """Open `path` read-only and check it is an index of this schema. Returns (conn, problem)."""
    if not path.exists():
        return None, "missing"
    if not path.is_file():
        return None, "not a file"
    uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(uri, uri=True)
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = sorted(REQUIRED_TABLES - tables)
        if missing:
            conn.close()
            return None, "not an os-optics-lens index (missing tables: " + ", ".join(missing) + ")"
        row = conn.execute("SELECT value FROM meta WHERE key='index_schema_version'").fetchone()
        version = row[0] if row else None
        if version != INDEX_SCHEMA_VERSION:
            conn.close()
            return None, f"schema mismatch: index_schema_version {version!r}, expected {INDEX_SCHEMA_VERSION!r}"
        mismatch = _semantics_mismatch(conn)
        if mismatch is not None:
            conn.close()
            return None, mismatch
    except sqlite3.Error as exc:
        if conn is not None:
            conn.close()
        return None, f"corrupt or not an SQLite index: {exc}"
    conn.row_factory = sqlite3.Row
    return conn, None


class Index:
    """A read-only, opened index. Use as a context manager or call `close()`."""

    def __init__(self, conn: sqlite3.Connection, path: Path, source: str,
                 stack: contextlib.ExitStack | None = None) -> None:
        self._conn = conn
        self.path = path
        self.source = source
        self._stack = stack
        self._meta: dict[str, str] | None = None
        self._parsed_evidence: dict[tuple[str, str], Any] = {}

    # Opening --------------------------------------------------------------------------------

    @classmethod
    def open(cls, path: Path | str | None = None) -> "Index":
        """Open the index named by `path`, else `$OS_OPTICS_LENS_INDEX`, else the bundled index."""
        tried: list[dict[str, Any]] = []
        if path is not None:
            candidate = Path(path)
            conn, problem = _connect(candidate)
            if conn is None:
                tried.append({"source": "argument", "path": str(candidate), "problem": problem})
                raise _unavailable(f"index {candidate} is unavailable: {problem}", tried)
            return cls(conn, candidate.resolve(), "argument")
        env_value = os.environ.get(ENV_VAR)
        if env_value:
            candidate = Path(env_value)
            conn, problem = _connect(candidate)
            if conn is None:
                tried.append({"source": "env", "path": str(candidate), "problem": problem})
                raise _unavailable(f"index {candidate} (from {ENV_VAR}) is unavailable: {problem}", tried)
            return cls(conn, candidate.resolve(), "env")
        stack = contextlib.ExitStack()
        try:
            resource = resources.files(__package__).joinpath("_data").joinpath("index.sqlite")
            if not resource.is_file():
                tried.append({"source": "bundled", "path": f"{__package__}/_data/index.sqlite", "problem": "missing"})
                raise _unavailable("no index is available: no --index, no " + ENV_VAR + " and no bundled index", tried)
            candidate = stack.enter_context(resources.as_file(resource))
            conn, problem = _connect(Path(candidate))
            if conn is None:
                tried.append({"source": "bundled", "path": str(candidate), "problem": problem})
                raise _unavailable(f"the bundled index is unavailable: {problem}", tried)
        except BaseException:
            stack.close()
            raise
        return cls(conn, Path(candidate).resolve(), "bundled", stack)

    def close(self) -> None:
        """Close the connection (and release a bundled-resource extraction, if any)."""
        self._conn.close()
        if self._stack is not None:
            self._stack.close()
            self._stack = None

    def __enter__(self) -> "Index":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def connection(self) -> sqlite3.Connection:
        """The underlying read-only connection (rows are `sqlite3.Row`)."""
        return self._conn

    def _rows(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> list[dict[str, Any]]:
        return [dict(row) for row in self._conn.execute(sql, params)]

    # Meta -----------------------------------------------------------------------------------

    def meta(self) -> dict[str, str]:
        """All meta key/value pairs."""
        if self._meta is None:
            self._meta = {row["key"]: row["value"] for row in self._conn.execute("SELECT key, value FROM meta ORDER BY key")}
        return dict(self._meta)

    @property
    def snapshot_id(self) -> str:
        """The index snapshot id (16 hex characters)."""
        return self.meta()["snapshot_id"]

    @property
    def record_count(self) -> int:
        """Number of indexed records."""
        return int(self.meta()["record_count"])

    @property
    def configuration_count(self) -> int:
        """Number of indexed configurations."""
        return int(self.meta()["configuration_count"])

    # Records and configurations -------------------------------------------------------------

    _RECORD_COLUMNS = ("record_pk, record_id, revision, record_schema_version, title, collection_id, family_id, "
                       "design_scope, record_hash, record_path, project_path, project_sha256, engine_version, "
                       "configuration_count")

    def iter_records(self, with_json: bool = False) -> Iterator[dict[str, Any]]:
        """Record rows ordered by record_id; with `with_json`, `record` holds the parsed record."""
        columns = self._RECORD_COLUMNS + (", record_json" if with_json else "")
        for row in self._conn.execute(f"SELECT {columns} FROM records ORDER BY record_id"):
            item = dict(row)
            if with_json:
                item["record"] = json.loads(item.pop("record_json"))
            yield item

    def record_ids(self) -> list[str]:
        """All record ids, sorted."""
        return [row[0] for row in self._conn.execute("SELECT record_id FROM records ORDER BY record_id")]

    def get_record(self, record_id: str, revision: int | None = None) -> dict[str, Any]:
        """The record row with `record` (parsed record.json).

        Raises RECORD_NOT_FOUND (details.did_you_mean: up to 5 close ids) or, when `revision` differs
        from the indexed revision, REVISION_NOT_AVAILABLE (details.available_revision).
        """
        row = self._conn.execute(f"SELECT {self._RECORD_COLUMNS}, record_json FROM records WHERE record_id=?",
                                 (record_id,)).fetchone()
        if row is None:
            close = difflib.get_close_matches(record_id, self.record_ids(), n=5, cutoff=0.5)
            raise LensError(
                "RECORD_NOT_FOUND",
                f"no record {record_id!r} in index snapshot {self.snapshot_id}",
                hint="Check the id, or find ids with `os-optics-lens search` or `os-optics-lens facets`.",
                details={"record_id": record_id, "did_you_mean": close},
            )
        item = dict(row)
        item["record"] = json.loads(item.pop("record_json"))
        if revision is not None and revision != item["revision"]:
            raise LensError(
                "REVISION_NOT_AVAILABLE",
                f"record {record_id!r} revision {revision} is not in this index; it holds revision {item['revision']}",
                hint="Omit --revision to use the indexed revision, or use an index built from that revision.",
                details={"record_id": record_id, "requested_revision": revision,
                         "available_revision": item["revision"]},
            )
        return item

    def record_by_pk(self, record_pk: int) -> dict[str, Any]:
        """The record row (without JSON) for a record_pk."""
        row = self._conn.execute(f"SELECT {self._RECORD_COLUMNS} FROM records WHERE record_pk=?", (record_pk,)).fetchone()
        if row is None:
            raise LensError("INTERNAL", f"record_pk {record_pk} is not in the index")
        return dict(row)

    def record_json(self, record_pk: int) -> dict[str, Any]:
        """The parsed record.json of a record."""
        row = self._conn.execute("SELECT record_json FROM records WHERE record_pk=?", (record_pk,)).fetchone()
        if row is None:
            raise LensError("INTERNAL", f"record_pk {record_pk} is not in the index")
        return json.loads(row[0])

    @staticmethod
    def _configuration(row: sqlite3.Row, with_view: bool) -> dict[str, Any]:
        item = dict(row)
        item["configuration_id"] = item["configuration_key"] or None
        item["native"] = bool(item["native"])
        view_json = item.pop("view_json", None)
        if with_view and view_json is not None:
            item["view"] = json.loads(view_json)
        return item

    def configurations(self, record_pk: int, with_view: bool = True) -> list[dict[str, Any]]:
        """Configurations of a record by ordinal: config_pk, record_pk, configuration_key,
        configuration_id (None for the Project as written), native, label, ordinal, view."""
        rows = self._conn.execute("SELECT * FROM configurations WHERE record_pk=? ORDER BY ordinal, config_pk",
                                  (record_pk,))
        return [self._configuration(row, with_view) for row in rows]

    def configuration(self, record_pk: int, configuration_id: str | None) -> dict[str, Any]:
        """One configuration by native id (None = the record's only/first configuration).

        Raises CONFIGURATION_NOT_FOUND listing the available ids.
        """
        items = self.configurations(record_pk)
        if configuration_id is None:
            return items[0]
        for item in items:
            if item["configuration_id"] == configuration_id:
                return item
        available = [item["configuration_id"] for item in items]
        record_id = self.record_by_pk(record_pk)["record_id"]
        raise LensError(
            "CONFIGURATION_NOT_FOUND",
            f"record {record_id!r} has no configuration {configuration_id!r}",
            hint="Use one of details.available (null means the Project as written).",
            details={"record_id": record_id, "configuration_id": configuration_id, "available": available},
        )

    def iter_configurations(self, with_view: bool = True) -> Iterator[dict[str, Any]]:
        """Every configuration joined with its record_id and revision, ordered by record_id, ordinal."""
        sql = ("SELECT c.*, r.record_id AS record_id, r.revision AS revision FROM configurations c "
               "JOIN records r ON r.record_pk = c.record_pk ORDER BY r.record_id, c.ordinal, c.config_pk")
        for row in self._conn.execute(sql):
            yield self._configuration(row, with_view)

    # Facts and facets -----------------------------------------------------------------------

    def facts(self, *, record_pk: int | None = None, config_pk: int | None = None, fact_key: str | None = None,
              fact_keys: list[str] | None = None, key_prefix: str | None = None) -> list[dict[str, Any]]:
        """Fact rows matching every given filter, ordered by fact_pk (record, configuration, key, path).

        Rows carry the facts-table columns; `system_aperture` is a bool or None.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if record_pk is not None:
            clauses.append("record_pk=?")
            params.append(record_pk)
        if config_pk is not None:
            clauses.append("config_pk=?")
            params.append(config_pk)
        if fact_key is not None:
            clauses.append("fact_key=?")
            params.append(fact_key)
        if fact_keys is not None:
            if not fact_keys:
                return []
            clauses.append(f"fact_key IN ({','.join('?' * len(fact_keys))})")
            params.extend(fact_keys)
        if key_prefix is not None:
            clauses.append("substr(fact_key, 1, ?) = ?")
            params.extend([len(key_prefix), key_prefix])
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._rows(f"SELECT * FROM facts{where} ORDER BY fact_pk", params)
        for row in rows:
            if row["system_aperture"] is not None:
                row["system_aperture"] = bool(row["system_aperture"])
        return rows

    def fact_keys(self) -> list[str]:
        """Every distinct fact key in the index, sorted."""
        return [row[0] for row in self._conn.execute("SELECT DISTINCT fact_key FROM facts ORDER BY fact_key")]

    def facet_rows(self, *, facet: str | None = None, values: list[str] | None = None,
                   record_pk: int | None = None) -> list[dict[str, Any]]:
        """Facet rows (record_pk, config_pk, facet, value, basis) in insertion order.

        `config_pk` is None for record-level facets and set for configuration-level facets.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if facet is not None:
            clauses.append("facet=?")
            params.append(facet)
        if values is not None:
            if not values:
                return []
            clauses.append(f"value IN ({','.join('?' * len(values))})")
            params.extend(values)
        if record_pk is not None:
            clauses.append("record_pk=?")
            params.append(record_pk)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return self._rows(f"SELECT record_pk, config_pk, facet, value, basis FROM facets{where} ORDER BY rowid", params)

    def facet_values(self, facet: str) -> list[str]:
        """Distinct values present for a facet, sorted."""
        return [row[0] for row in self._conn.execute("SELECT DISTINCT value FROM facets WHERE facet=? ORDER BY value",
                                                     (facet,))]

    # Evidence -------------------------------------------------------------------------------

    def evidence_refs(self, record_pk: int) -> list[dict[str, Any]]:
        """Evidence reference rows of a record ordered by field. `status` is the LIVE status (see
        `live_ref_status`); `build_status` is the one recorded at build."""
        rows = self._rows("SELECT * FROM evidence_refs WHERE record_pk=? ORDER BY field", (record_pk,))
        return [self._with_live_status(row) for row in rows]

    def evidence_ref(self, ref_pk: int) -> dict[str, Any] | None:
        """One evidence reference row (as `evidence_refs`), or None."""
        rows = self._rows("SELECT * FROM evidence_refs WHERE ref_pk=?", (ref_pk,))
        return self._with_live_status(rows[0]) if rows else None

    def _with_live_status(self, row: dict[str, Any]) -> dict[str, Any]:
        row["build_status"] = row["status"]
        row["status"] = self.live_ref_status(row)
        return row

    def live_ref_status(self, ref: Mapping[str, Any]) -> str:
        """The status of a reference row (with its build `status`) against the bytes the index
        holds now.

        A reference that reached a stored file at build (RESOLVED, SECTION_MISSING) is resolved
        again from verified bytes. A reference the build rejected keeps that status, and so does a
        license notice reference (it names no sha256).
        """
        status = ref["status"]
        if status not in ("RESOLVED", "SECTION_MISSING") or ref["field"] == NOTICE_FIELD:
            return status
        if ref["section"] is not None:
            return self.resolve_source({key: ref[key] for key in ("path", "sha256", "section")})["status"]
        return self.evidence_state(ref["path"], ref["sha256"])[0]

    def _evidence_lookup(self, path: str) -> tuple[Any, Any] | None:
        """(stored sha256, stored content) of an evidence file, unverified, or None."""
        try:
            row = self._conn.execute("SELECT sha256, content FROM evidence_files WHERE path=?",
                                     (path,)).fetchone()
        except sqlite3.Error:  # e.g. content rewritten as text that is not UTF-8
            return None, None
        return (row[0], row[1]) if row else None

    def evidence_state(self, path: Any, sha256: Any) -> tuple[str, bytes | None]:
        """(status, content) of the evidence file `path` expected to hash to `sha256`: RESOLVED
        with its LF bytes only when the bytes hash to `sha256` and the stored digest column equals
        it too, else PATH_ESCAPES, FILE_MISSING or HASH_MISMATCH with None."""
        rel_path = corpus_relative_path(path)
        if rel_path is None:
            return "PATH_ESCAPES", None
        found = self._evidence_lookup(rel_path)
        if found is None:
            return "FILE_MISSING", None
        stored, content = found
        if stored != sha256 or not _hashes_to(content, sha256):
            return "HASH_MISMATCH", None
        return "RESOLVED", content

    def evidence_damaged(self, path: str) -> bool:
        """True when a stored evidence file's bytes no longer hash to its own digest column."""
        found = self._evidence_lookup(path)
        return found is not None and not _hashes_to(found[1], found[0])

    def evidence_file(self, path: str, sha256: str) -> bytes | None:
        """LF bytes of a verified evidence file with that path and sha256, else None."""
        return self.evidence_state(path, sha256)[1]

    def evidence_paths(self) -> list[str]:
        """Paths of all stored evidence files, sorted."""
        return [row[0] for row in self._conn.execute("SELECT path FROM evidence_files ORDER BY path")]

    def resolve_source(self, source: Any) -> dict[str, Any]:
        """`resolve_source_in` against this index's evidence files (bytes verified on every call)."""
        return resolve_source_in(self._evidence_lookup, source, self._parsed_evidence)

    # Projects, licenses, findings -------------------------------------------------------------

    def project(self, record_pk: int) -> dict[str, Any]:
        """Project metadata: sha256, size_bytes, native_configuration_ids (list)."""
        row = self._conn.execute(
            "SELECT sha256, length(content) AS size_bytes, native_configuration_ids FROM projects WHERE record_pk=?",
            (record_pk,)).fetchone()
        if row is None:
            raise LensError("INTERNAL", f"record_pk {record_pk} has no Project in the index")
        return {"sha256": row["sha256"], "size_bytes": row["size_bytes"],
                "native_configuration_ids": json.loads(row["native_configuration_ids"] or "[]")}

    def project_bytes(self, record_pk: int) -> bytes:
        """The native Project's exact LF bytes, re-verified against the stored sha256 on every read
        (SQLite has no page checksums). A mismatch raises INDEX_UNAVAILABLE: the index is damaged."""
        row = self._conn.execute("SELECT sha256, content FROM projects WHERE record_pk=?", (record_pk,)).fetchone()
        if row is None:
            raise LensError("INTERNAL", f"record_pk {record_pk} has no Project in the index")
        content = bytes(row[1])
        actual = sha256_hex(content)
        if actual != row[0]:
            raise LensError(
                "INDEX_UNAVAILABLE", f"the index {self.path} is damaged: a stored Project does not match "
                "its sha256", hint="Rebuild the index with `os-optics-lens index build --corpus <dir> --out <file>`.",
                details={"tried": [{"source": self.source, "path": str(self.path),
                                    "problem": "damaged: a stored Project does not match its sha256"}],
                         "path": str(self.path), "record_pk": record_pk, "sha256": row[0], "actual_sha256": actual})
        return content

    def licenses(self) -> dict[str, str]:
        """License texts by corpus-relative path."""
        return {row[0]: row[1] for row in self._conn.execute("SELECT path, content FROM licenses ORDER BY path")}

    def findings(self) -> list[dict[str, Any]]:
        """Warnings recorded at build time, sorted by file, field, code."""
        return self._rows("SELECT * FROM findings ORDER BY file, field, code, record_id, message")


def open_index(path: Path | str | None = None) -> Index:
    """Open an index by the documented resolution order (see `Index.open`)."""
    return Index.open(path)

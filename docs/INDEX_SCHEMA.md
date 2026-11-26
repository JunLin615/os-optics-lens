# Index schema, build findings and reproducibility

The retrieval index is a single SQLite file **derived** from the committed records. The records
and their native Projects remain the source of truth; an index file is never committed. This
document describes index schema version `2` as `os_optics_lens.build` writes it and
`os_optics_lens.store` reads it, the findings a build reports, the reproducibility rules, and how
to validate an incoming batch. [RECORD_FORMAT.md](RECORD_FORMAT.md) defines the records the index
reads; [RETRIEVAL.md](RETRIEVAL.md) describes the CLI and the query semantics.

## Building and locating an index

```
os-optics-lens index build    [--corpus DIR] [--out FILE] [--allow-unresolved-evidence]
os-optics-lens index validate [--corpus DIR] [--allow-unresolved-evidence]
os-optics-lens index dump     [--index FILE]
```

- `--corpus` is the directory that contains `records/` (default `.`). `--out` defaults to
  `build/os-optics-lens-index.sqlite`.
- `index build` writes `<out>.tmp-<pid>` in the output directory. It inserts every row in sorted
  order, commits, checks `PRAGMA integrity_check`, runs `VACUUM`, closes the connection and then
  `os.replace`s the temporary file onto `<out>` (`page_size=4096`, `journal_mode=DELETE`).
  - Any failure removes the temporary file, so a previous `<out>` stays byte-identical.
  - On Windows, a reader that still holds `<out>` open makes the replace fail. The result is
    `INDEX_IN_USE` (exit code 5), and the old file is kept. An `<out>` that is a directory (checked
    before the build) or a read-only file is `INVALID_REQUEST` (exit code 2) instead.
  - On success the envelope carries `snapshot` (the new index) and `result {out, snapshot_id, records,
    configurations, facts, warnings, findings, findings_omitted, use: "--index <out>"}`. `out` is
    absolute; `findings` lists at most 50 warning findings (`findings_omitted` counts the rest).
    A failed build reports `BUILD_FAILED` with at most 50 findings, errors first, in
    `details.findings` and the remainder count in `details.findings_omitted`. The envelope stays
    within 32768 bytes: long `expected`/`actual` values and messages are shortened and findings
    that still do not fit are counted in `findings_omitted` (`details.truncated: true`).
- `index validate` runs the complete build into a temporary file, discards it and reports the same
  result, with `out` and `use` set to null.
- `index dump` prints the canonical JSON of every table (see [Reproducibility](#reproducibility)).
- Readers open an index with `os_optics_lens.store.Index.open(path=None)` and use the first source
  that is specified, without falling back to another:
  1. the `--index FILE` argument;
  2. the `OS_OPTICS_LENS_INDEX` environment variable;
  3. the wheel's bundled `os_optics_lens/_data/index.sqlite`.

  There is no lookup in the working directory. The file is opened read-only as
  `file:<absolute path>?mode=ro&immutable=1`, so read-only installs and read-only trees work and
  no `-wal`/`-shm`/journal sidecars are created. A missing file, a corrupt file, a missing table,
  a different `index_schema_version`, or a `vocabulary_digest` or `index_semantics` that differs
  from the reader's (see [Reader compatibility](#reader-compatibility)) raises `INDEX_UNAVAILABLE`
  (exit code 5). Its `details.tried` lists `[{source, path, problem}]`, and its hint names
  `index build --corpus <dir> --out <file>`.

## Tables

Primary keys (`record_pk`, `config_pk`, `fact_pk`, `ref_pk`) are dense integers that start at 1 and
follow the insertion order below. They are stable for one snapshot only. Across snapshots, refer
to records by `record_id` and to configurations by `configuration_id`.

### `meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)`

| key | value |
|---|---|
| `index_schema_version` | `"2"`; readers refuse any other value (version 1 lacked `facts.sampled_field_fractions_json`) |
| `builder` | `"os-optics-lens <version>"` |
| `builder_version` | package version, e.g. `"0.1.0"` |
| `snapshot_id` | 16 hex characters (see [Snapshot identity](#snapshot-identity)) |
| `record_count`, `configuration_count`, `fact_count` | decimal counts |
| `content_digest` | SHA-256 of the canonical dump of every table except `meta` |
| `corpus_digest` | SHA-256 of the canonical JSON of the sorted rows `[record_id, revision, record_hash, project_sha256, [[evidence path, sha256], ...]]` |
| `allow_unresolved_evidence` | `"0"` or `"1"` |
| `vocabulary_digest` | SHA-256 of the canonical JSON of the packaged `vocabulary.json` |
| `index_semantics` | fingerprint of the index-producing sources (see [Reader compatibility](#reader-compatibility)) |

### `records`

One row per indexed record, ordered by `record_id`.

| column | meaning |
|---|---|
| `record_pk INTEGER PRIMARY KEY` | row key |
| `record_id TEXT UNIQUE NOT NULL` | the record's stable identity |
| `revision INTEGER NOT NULL` | `record_revision`. The index holds exactly one revision per record |
| `record_schema_version TEXT` | `"1.0"` or `"1.1"` |
| `title`, `collection_id`, `family_id`, `design_scope` | copied from the record. `family_id` is null when `design.design_family_id` is absent |
| `record_hash TEXT` | the stated `record_hash`, verified at build time |
| `record_path TEXT` | corpus-relative POSIX path of `record.json` |
| `project_path TEXT`, `project_sha256 TEXT` | the normalized `native_project.path` and the SHA-256 of the Project's LF bytes (equal to `native_project.sha256`) |
| `engine_version TEXT` | `native_project.engine_version` |
| `configuration_count INTEGER` | number of indexed configurations |
| `record_json TEXT NOT NULL` | Canonical JSON of the record and native Project identity plus allowlisted direct-source metadata for `inspect --provenance`. Configuration facts live in `view_json`. |

### `configurations`

A candidate unit for search is one (record, configuration). Each row carries the configuration
view, which constraint evaluation reads.

| column | meaning |
|---|---|
| `config_pk INTEGER PRIMARY KEY` | row key |
| `record_pk INTEGER NOT NULL` | owning record |
| `configuration_key TEXT NOT NULL` | the native `configuration_id`, or `''` for the Project as written. `UNIQUE(record_pk, configuration_key)` |
| `native INTEGER NOT NULL` | 1 when the row names a configuration the native Project declares |
| `label TEXT` | entry label, else the Project's label, else the id. `"project as written"` for the non-native row |
| `ordinal INTEGER NOT NULL` | 0-based position in the record's `configurations` (0 for the single row) |
| `view_json TEXT NOT NULL` | canonical JSON of the configuration view (see below) |

The configuration view projects the record onto Project facts and retrieval metadata. It omits
source optical values and historical intake checks. Each section in the configuration entry
replaces the top-level section of the same name, and `conjugate` replaces `design.conjugate`.
`view.configuration = {configuration_id, label, native}` identifies the configuration.

A record without `configurations` has exactly one row:

- If its native Project declares exactly one configuration, the row takes that id
  (`native = 1`).
- Otherwise the row has `configuration_key = ''`, and its `configuration_id` is JSON `null` in all
  output.

### `facets`

`facets(record_pk INTEGER NOT NULL, config_pk INTEGER, facet TEXT NOT NULL, value TEXT NOT NULL,
basis TEXT NOT NULL)`, indexed on `(facet, value)` and `(record_pk, config_pk)`.

- **Record-level** facets have `config_pk` NULL: `collection`, `family`, `design_scope`,
  `design_type`, `source_lens_type`, `function`, `application`, `structure`, `module_role`,
  `stop_state`.
- **Configuration-level** facets have `config_pk` set: `band`, `image_format`, `conjugate`,
  `field_kind`, `system_aperture`, `evidence`.
- `value` is the canonical lower_snake_case term. `source_lens_type` is kept verbatim. An
  unknown tag is indexed normalized and an unknown `module_role` verbatim, both with an
  `UNKNOWN_TERM` warning.
- `basis` is `RECORD` when the value is copied from the record and `VOCABULARY_MAPPING` when it is
  derived through `vocabulary.json`, for example `design_type` mapped from `lens_type`, or the
  `imaging` function.
- `band` is discovery-only. It is derived from the sampling span and never decides coverage.

### `facts`

One typed quantity at one JSON location of one configuration view, extracted generically (spec
§4, amendments 10.7 and 10.8). Rows are ordered by (record, configuration, `fact_key`, `json_path`).
The table is indexed on `(fact_key, value_num)` and `(config_pk)`. Facts serve filtering, ranges,
sorting, `fields`, `compare` and `inspect`. **Constraint evaluation reads `view_json`, not this
table.** Both come from the same view, so they cannot disagree.

| column | meaning |
|---|---|
| `fact_pk INTEGER PRIMARY KEY`, `record_pk`, `config_pk` | keys (`config_pk` is never NULL) |
| `fact_key TEXT NOT NULL` | dotted scope path: `first_order.effective_focal_length_mm`, `aperture.f_number`, `image_format.cover_glass.thickness_mm`, `performance.<metric>`, `analysis_evidence.<study>`, … |
| `json_path TEXT NOT NULL` | location in the view: the value (`first_order.back_focal_length_mm.value`, `performance[2].value`), or the object itself for intervals and value-less entries (`spectrum.sampling_span_nm`, `performance[0]`) |
| `metric TEXT` | performance metric, else NULL |
| `value_num REAL` | the numeric value (performance: `value`, else the first `value_<suffix>`) |
| `value_min REAL`, `value_max REAL` | interval bounds (`min_nm`/`max_nm`, `min_<s>`/`max_<s>`) |
| `value_json TEXT` | canonical JSON of the raw value (numbers, lists, strings, booleans, a whole analysis study) |
| `unit TEXT` | from the member name: a name containing `_fraction_of_` (e.g. `field_fraction_of_stated_half_field`) is a ratio → `"1"`; otherwise `_lp_mm`→`lp/mm`, `_percent`, `_deg`, `_um`, `_nm`, `_mm`. f-number, numerical aperture, `_count`, `_fraction` and `_weight` names → `"1"`. Otherwise NULL |
| `basis TEXT`, `basis_class TEXT` | the fact's own `basis`, else the sibling `<stem>_basis`, else the section `basis` when it applies (amendment 10.7). `basis_class` is the leading token `PROJECT_DECLARED`, `PROJECT_DERIVED`, `DERIVED` or `ENGINE_COMPUTED`. Other labels use `OTHER` |
| `conditions_json TEXT` | canonical JSON of an object `conditions`. Integral floats are normalized to integers, so equal conditions compare equal as text |
| `conditions_text TEXT` | prose `conditions` string, verbatim (then `conditions_json` is NULL) |
| `definition TEXT` | `definition` / `condition` prose, verbatim |
| `eligible_for TEXT` | e.g. `DISCOVERY_ONLY` (`spectrum.sampling_span_nm`) |
| `state TEXT` | e.g. `continuous_coverage_nm.state`, an inherited `field.verification.state`, or an analysis study's `status` |
| `system_aperture INTEGER` | 1/0 for aperture facts that state it, else NULL |
| `qualifiers_json TEXT` | performance-entry scalar/list members that qualify the value (e.g. `field_fraction`, `frequency_lp_mm`) |
| `value_paths TEXT` | JSON list of numeric value-like members (`["value"]`, `["sagittal","tangential"]`, `["min_nm","max_nm"]`) |
| `constraint_form TEXT` | JSON `{scope, metric, path}`: the fixture-form constraint that addresses this fact |
| `sampled_field_fractions_json TEXT` | canonical JSON of the sampling grid (`sampled_field_fractions`) of the object the fact resolves to (a performance entry, member object, section or study), else NULL; a comparison condition for `fields` and `compare` |
| `source_ref_pk INTEGER` | the `evidence_refs` row of the fact's own `source`, else NULL |

Historical comparisons such as `engine_check`, `property_list_comparison`,
`prescription_dump_check`, `catalog_comparisons` and `catalog_comparison` are omitted from the
configuration view. An eligible engine result can be promoted to its ordinary Project fact key.

### `evidence_files(path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, content BLOB NOT NULL)`

These are the evidence extracts the records reference. `content` holds the LF-normalized bytes,
and `sha256` is their hash. Only files that passed verification are inserted: the path is inside
the corpus, the bytes match a referenced `sha256`, and the content follows the
[evidence extract contract](RECORD_FORMAT.md#evidence-extracts). At query time, evidence is
resolved only from this table (`store.resolve_source_in`), never from the filesystem.

SQLite has no page checksums, so **every read verifies the bytes**: their SHA-256 is recomputed and
must equal both the `sha256` column and the digest the reference expects. Otherwise the status is
`HASH_MISMATCH` (`FILE_MISSING` if the row is gone), a constraint that needs the evidence is
`UNCONFIRMED` (`EVIDENCE_UNRESOLVED`), and none of the bytes are parsed or shown. Only verified
content is cached. `inspect --evidence` of a record with damaged evidence fails with
`INDEX_UNAVAILABLE` (rebuild). A reference that was already unresolved at build is not damage.

### `evidence_refs`

`evidence_refs(ref_pk INTEGER PRIMARY KEY, record_pk INTEGER NOT NULL, field TEXT NOT NULL, path
TEXT, sha256 TEXT, section TEXT, status TEXT NOT NULL)`, indexed on `record_pk` and ordered by
`field` within a record.

| `field` | reference |
|---|---|
| any location of an object with `path` + `sha256` + `section`, and every object that is the value of a member named `source` (a reference missing one of the three keys is validated and reported, not skipped), e.g. `performance[4].source` (`configurations[i].` prefix inside configuration entries) | a fact source |
| `collection.evidence_extract` | the record's evidence extract (`path` + `sha256`, no section) |
| `collection.license.notice_path` | the license notice (`path` only) |

`status` is one of:

- `RESOLVED`;
- `FILE_MISSING`, `HASH_MISMATCH`, `SECTION_MISSING`, `MALFORMED` or `PATH_ESCAPES`;
- `NOTICE_MISMATCH`, which applies only to license notices.

A status other than `RESOLVED` can occur only in an index built with `--allow-unresolved-evidence`.
The exception is `PATH_ESCAPES`, which is always an error.

The column is the status **at build**. Every status a query reports is **live**
(`Index.evidence_refs`, which keeps the column as `build_status`): a reference that reached a
stored file at build (`RESOLVED`, `SECTION_MISSING`) is resolved again from the verified bytes of
`evidence_files` (amendment 10.4). A reference the build rejected keeps its status, and so does a
license notice reference, which names no `sha256`.

### `projects(record_pk INTEGER PRIMARY KEY, sha256 TEXT NOT NULL, content BLOB NOT NULL, native_configuration_ids TEXT)`

This table holds the exact LF bytes of each native Project; `os-optics-lens project --out` writes
them. `sha256` equals the record's `native_project.sha256`, which is checked at build time and
again on every read: `project` re-hashes `content` before writing anything, and a mismatch (a
damaged index) is `INDEX_UNAVAILABLE` with no output written.
`native_configuration_ids` is the JSON list of valid ids that the Project declares under
`configurations[].configuration_id` (`[]` when it declares none).

### `licenses(path TEXT PRIMARY KEY, content TEXT NOT NULL)`

This table holds the LF text of `LICENSE`, of `licenses/*.txt` and of every license notice a
record references.

### `findings(severity TEXT NOT NULL, code TEXT NOT NULL, record_id TEXT, file TEXT NOT NULL, field TEXT NOT NULL, message TEXT NOT NULL)`

These are the warnings that survived a successful build, ordered by (`file`, `field`, `code`). A
build with an error writes no index, so this table never holds errors.

## Snapshot identity

```
snapshot_id     = sha256(canonical_json({
                    "index_schema_version", "builder_version", "vocabulary_digest",
                    "allow_unresolved_evidence", "content_digest"}))[:16]
content_digest  = sha256(canonical_json(dump of every table except meta))
canonical_json  = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
```

The dump lists each table as `{columns, rows}`. Rows are ordered by every column, and BLOBs are
written as `{sha256, length}`. The snapshot therefore changes when anything indexed changes: a
record, a Project, an evidence extract, a license text, the vocabulary, the builder version or the
evidence policy. It does not depend on file timestamps, absolute paths, the platform or the SQLite
page layout.

Search cursors embed the snapshot. A cursor from another snapshot is rejected with
`SNAPSHOT_MISMATCH` (exit code 4).

## Reader compatibility

A snapshot identifies content. What the content and a result offset mean also depends on the code,
so both are bound to fingerprints (`os_optics_lens.semantics`): the SHA-256 over the name, length
and LF-normalized bytes of each file, read through `importlib.resources`, so a CRLF checkout, a
wheel and every platform agree.

| fingerprint | files | bound to |
|---|---|---|
| `index_semantics` (meta) | `build.py`, `canonical.py`, `corpus.py`, `normalize.py`, `vocabulary.py`, `vocabulary.json` | the index: a reader with another value, or another `vocabulary_digest`, refuses it with `INDEX_UNAVAILABLE` and a rebuild hint |
| reader fingerprint (first 16 hex in each cursor) | `canonical.py`, `evaluate.py`, `normalize.py`, `output.py`, `query.py`, `store.py`, `vocabulary.py`, `vocabulary.json` | the cursor |

A cursor is urlsafe base64 of `{"v": 2, "r": reader, "s": snapshot_id, "q": query_digest, "o":
offset}`. A cursor whose `r` differs from the reader's is `CURSOR_SEMANTICS_MISMATCH` (exit code
4), also against an index rebuilt by that reader; a version 1 cursor is `CURSOR_INVALID`. Both
hints say to restart the search without a cursor. Any edit of a listed file changes its
fingerprint, so an index built from a modified checkout is rebuilt, not reused.

## Reproducibility

- **Inputs are normalized.** Every byte hash (Project, evidence) is taken over LF-normalized bytes,
  so a `core.autocrlf=true` checkout hashes the same as the repository's LF form. `record_hash` is
  computed over the parsed JSON, exactly as RECORD_FORMAT.md states.
- **The written file is deterministic.** Rows are inserted in sorted order and all stored JSON is
  canonical. Nothing time-, environment- or path-dependent is stored: paths are corpus-relative
  POSIX paths. Two builds from the same inputs with the same Python/SQLite therefore produce
  **byte-identical** files.
- **The snapshot is portable.** Across Python versions, SQLite versions and platforms, the
  `snapshot_id` and `os-optics-lens index dump` are identical. Comparing them is the supported way
  to check that two indexes hold the same content.
- **The wheel bundles the index.** `hatch_build.py` copies every indexed `record.json`,
  `project.json` and evidence extract, every batch manifest, `LICENSE`, `licenses/*` and
  `acceptance/*.json`. The copies are LF-normalized, hash-verified and placed at their
  repository-relative paths under `os_optics_lens/_data/corpus/`. The hook then builds
  `os_optics_lens/_data/index.sqlite` **from that bundled copy** and fails the wheel build unless
  its `snapshot_id` equals the snapshot of an index built from the source tree. An installed
  wheel can therefore rebuild its own index:

  ```
  os-optics-lens index build --corpus <site-packages>/os_optics_lens/_data/corpus --out rebuilt.sqlite
  ```

  This reproduces the `snapshot_id` that `os-optics-lens info` reports, and
  `scripts/verify_wheel.py` checks it.
- Editable installs and the sdist contain no `_data/`. Use `--index` or `OS_OPTICS_LENS_INDEX`.

## Build findings

Every problem is a finding:

```
{"severity": "error" | "warning", "code", "record_id", "file", "field", "message", "expected"?, "actual"?}
```

- `file` is a corpus-relative POSIX path. It names `project.json` for problems inside the native
  Project.
- `field` is a dotted path with `[i]` list indices, such as `performance[4].source.sha256`. It is
  empty when the problem concerns the whole file.
- Findings are sorted by (`file`, `field`, `code`).

**Any error fails the build.** The result is `BUILD_FAILED` (exit code 6), with
`details.{errors, warnings, findings}`. No index is written, and a previous index stays
byte-identical. A record with an error is never indexed, and neither is either record of a
duplicate pair. Warnings are reported in the build output and stored in the `findings` table.

`--allow-unresolved-evidence` downgrades the codes marked † to warnings. Such a reference is then
stored with its failure status, so every constraint that depends on it is `UNCONFIRMED`
(`EVIDENCE_UNRESOLVED`). Use the flag only to index a record whose evidence you deliberately left
unresolved; the flag is recorded in `meta` and in the snapshot.

### Record and file structure

| code | severity | meaning |
|---|---|---|
| `MALFORMED_JSON` | error | Not parseable, not UTF-8, has a BOM, contains `NaN`/`Infinity`/an overflowing number (a float or an integer beyond the double range), a string or member name with a lone surrogate escape, a member name repeated within one object, or nesting deeper than 128 levels (field = JSON pointer location), or the top level is not an object (the literal `null` included; no discovered file is skipped). Applies to records and batch manifests; the same defects in a Project or an evidence file are `PROJECT_MALFORMED` and `EVIDENCE_MALFORMED`. |
| `MISSING_FIELD` | error | A required field is absent: `schema`, `record_schema_version`, `record_id`, `record_revision`, `title`, `collection`, `collection.collection_id`, `design`, `native_project`, `native_project.path`, `native_project.sha256`, `spectrum`, `record_hash`, `record_hash_method`; a performance entry's `metric`; a configuration entry's `configuration_id`; a batch manifest's `collection` and `collection.collection_id`. |
| `INVALID_FIELD` | error | The wrong type or value. Includes: `schema` ≠ `opensci.lens.record`; a `record_id` not matching `^[a-z0-9][a-z0-9._-]{0,127}$`; `record_revision` not an int from 1 to 2^63-1; a tag list that is not a list of strings; a numeric fact `value` (also performance `value*`/`min_*`/`max_*` and spectrum `min_nm`/`max_nm`) that is not a finite number or null; `performance` not a list; a 1.0 record carrying `configurations`; malformed or unknown configuration-entry keys; a top-level section that some configuration would inherit; any member of [RECORD_FORMAT.md, Member types](RECORD_FORMAT.md#member-types) with another type, at the top level or in a configuration entry. |
| `UNSUPPORTED_SCHEMA_VERSION` | error | `record_schema_version` is not `1.0` or `1.1`. |
| `RECORD_DIR_MISMATCH` | error | The record directory name differs from `record_id`. |
| `COLLECTION_DIR_MISMATCH` | warning | The collection directory differs from `collection.collection_id`. |
| `DUPLICATE_RECORD_ID` | error | The same `record_id` appears in two files. Reported on every occurrence, and neither record is indexed. |
| `RECORD_HASH_MISMATCH` | error | The recomputed `record_hash` differs (`expected` = recomputed, `actual` = stated). |
| `MISSING_FAMILY` | warning | `design.design_family_id` is absent. |

### Native Project and configurations

| code | severity | meaning |
|---|---|---|
| `PROJECT_MISSING` | error | `native_project.path` names no file. |
| `PROJECT_HASH_MISMATCH` | error | The SHA-256 of the Project's LF bytes differs from `native_project.sha256`. |
| `PROJECT_MALFORMED` | error | The Project is not a JSON object with `schema == "opensci.project"`. |
| `PROJECT_PATH_ESCAPES` | error (never downgraded) | A Project, evidence or notice path is absolute or resolves outside the corpus root. |
| `PROJECT_CONFIGURATION_INVALID` | error | A native `configurations[].configuration_id` is not a string matching `^[A-Za-z][A-Za-z0-9_-]{0,63}$`, duplicates another id case-insensitively, or is a reserved device name (`con`, `prn`, `aux`, `nul`, `com1`–`com9`, `lpt1`–`lpt9`). Reported against `project.json`. |
| `CONFIGURATION_UNKNOWN` | error | A record `configurations[].configuration_id` is not declared by the native Project. Ids must match exactly. |
| `DUPLICATE_CONFIGURATION_ID` | error | Two record configuration entries share an id (compared case-insensitively). |
| `CONFIGURATION_UNMAPPED` | error | The Project declares two or more configurations, and the record has no `configurations`. |
| `CONFIGURATION_COUNT_MISMATCH` | error | `design.configuration_count` differs from the number of indexed configurations. |

### Evidence and licenses

| code | severity | meaning |
|---|---|---|
| `EVIDENCE_FILE_MISSING` † | error | A referenced evidence file does not exist. |
| `EVIDENCE_HASH_MISMATCH` † | error | The SHA-256 of the file's LF bytes differs from the reference's `sha256`. |
| `EVIDENCE_SECTION_MISSING` † | error | The file has no `sections.<section>`. |
| `EVIDENCE_MALFORMED` † | error | The reference has an empty or missing `path`/`section` or a non-hex `sha256`; a `source` member at any depth of the fact data (lists and `configurations[]` entries included) or `collection.evidence_extract` is not an object; or the file breaks the [evidence extract contract](RECORD_FORMAT.md#evidence-extracts) (the message names the file and the field: root, `schema`, `sections`, `sections.<name>`, or, for the file named by `collection.evidence_extract`, a `record_id` that is not the string naming that record). |
| `LICENSE_NOTICE_MISSING` † | error | `collection.license.notice_path` is null, empty or not a string, or names no UTF-8 text file. |
| `LICENSE_NOTICE_MISMATCH` † | error | The notice does not contain `collection.license.copyright`. |
| `LICENSE_UNREADABLE` | error | `LICENSE` or `licenses/*.txt` is not UTF-8 text. |

### Batch manifests, vocabulary and facts

| code | severity | meaning |
|---|---|---|
| `BATCH_MANIFEST_MISMATCH` | error | A manifest's `accepted_record_ids` names a record that is not present in its collection. |
| `BATCH_MANIFEST_MISMATCH` | warning | A manifest's `collection.collection_id` differs from its directory; or an indexed record is absent from every manifest of its collection (only when the collection has at least one manifest). |
| `UNKNOWN_TERM` | warning | A `lens_type`, tag or `module_role` is not in the vocabulary. The value is still indexed (lens type under `source_lens_type` only, a tag normalized, a module role verbatim), so new series work before the vocabulary catches up. |
| `NO_SYSTEM_APERTURE` | warning | The configuration has neither a declared aperture in the record nor a system aperture in the Project projection. A legacy declaration suppresses this intake warning without making its value searchable. |
| `UNIT_UNKNOWN` | warning | A numeric fact outside `design.*` has no unit inferable from its name. |

Build-level errors that are not findings: `INVALID_REQUEST` (exit code 2) when `--corpus` has no
`records/` directory or `--out` is a directory or a read-only file; `INDEX_IN_USE` (5); `INTERNAL` (1) if the new file fails its integrity check.

## Validating an incoming batch

A new series (one collection directory, or records added to an existing one) is ready when a
**strict** build of the whole corpus has no errors and its warnings are understood.

1. Lay the files out as [RECORD_FORMAT.md](RECORD_FORMAT.md) requires:
   - `records/<collection_id>/<record_id>/record.json` and `project.json`;
   - evidence extracts, referenced by path and SHA-256 from the record;
   - a batch manifest `records/<collection_id>/batch-*.json` whose `accepted_record_ids` lists every
     accepted record;
   - any license notice under `licenses/`, with the record's `collection.license.notice_path`
     pointing to it.

   Compute `native_project.sha256` and every evidence `sha256` over LF bytes, and compute
   `record_hash` over the parsed record (the `os_optics_lens.canonical` helpers `sha256_lf` and
   `record_hash` implement both).
2. Validate the whole corpus strictly from the repository root:

   ```
   os-optics-lens index validate --corpus .
   # from a checkout without installing:  PYTHONPATH=src python -m os_optics_lens index validate --corpus .
   ```

   Each finding names the `file` and `field` to fix. Do not use `--allow-unresolved-evidence` to
   pass a batch; a reference that cannot be resolved should be fixed or removed.
3. Review the warnings:
   - `UNKNOWN_TERM`: add the term (or an alias) to `src/os_optics_lens/vocabulary.json` if it is a
     real new category.
   - `NO_SYSTEM_APERTURE` and `UNIT_UNKNOWN`: name the aperture or rename the member with a unit
     suffix if the value is meant to be filtered.
   - `BATCH_MANIFEST_MISMATCH`: list the record in its manifest.
4. Build an index and query the new records:

   ```
   os-optics-lens index build --corpus . --out build/os-optics-lens-index.sqlite
   os-optics-lens --index build/os-optics-lens-index.sqlite facets --facet collection
   os-optics-lens --index build/os-optics-lens-index.sqlite inspect <record_id>
   ```

   The snapshot changes, and the records of the batch are counted under their collection.
5. Run the tests (`PYTHONPATH=src python -m unittest discover -s tests -v`). Before a release,
   build the wheel (`uv build --wheel`) and run `python scripts/verify_wheel.py <wheel> --scratch
   <dir outside the checkout>`. The script installs the wheel into a clean venv, uses no network,
   and checks the bundled index, the hand-off and the rebuild snapshot.

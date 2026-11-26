# Errors and exit codes

A failed command prints `{"ok": false, "error": {"code", "message", "hint", "details"}}` on
stdout and exits with the code below. `details.pointer` (a JSON pointer into the request) locates
request problems; `details.did_you_mean` suggests close names. Nothing is printed on stderr
unless `OS_OPTICS_LENS_DEBUG=1` is set (then INTERNAL errors print their traceback there). The
error document fits the budget too (`max_output_bytes`, default 32768): long echoed values are
shortened and `details.truncated` is true when anything was cut.

| code | exit | meaning | what to do |
|---|---|---|---|
| `INVALID_REQUEST` | 2 | malformed JSON (also a key repeated in one object), unknown key, wrong type, out-of-range option (`details.allowed`), bad command line (`details.argv_error`, `details.usage`), an `--out` path that cannot be written (`details.path`, `details.os_error`) | fix the named field or path; never clamped |
| `UNKNOWN_FACET` | 2 | no such facet | `os-optics-lens facets` lists them |
| `UNKNOWN_FACET_VALUE` | 2 | value is neither a vocabulary term nor present in the index | use `details.did_you_mean` |
| `AMBIGUOUS_TERM` | 2 | an alias maps to several terms (e.g. `objective`) | pick one of `details.candidates` |
| `UNKNOWN_FIELD` | 2 | fact key not in the index (ranges, targets, preferences, `fields --fact`, compare/inspect fields), or a name of a provenance check (`details.provenance_check`), which cannot be matched or ranked | `os-optics-lens fields`; see `did_you_mean`; use `inspect ID --provenance` for direct-source information |
| `UNSUPPORTED_OPERATOR` | 2 | unknown `op` | pick one of `details.supported` |
| `CURSOR_INVALID` | 2 | the cursor is not a cursor, or a version 1 cursor | pass `next_cursor` exactly; for an old cursor restart without it |
| `CURSOR_MISMATCH` | 2 | the cursor belongs to another query | resend the same query with it |
| `OUTPUT_LIMIT_EXCEEDED` | 2 | the response cannot fit `max_output_bytes` (`details.required_bytes`) | raise the budget (max 262144), lower `limit`, ask for fewer fields/sections |
| `FILE_EXISTS` | 2 | `project --out` target exists | add `--force` or choose another path |
| `EVIDENCE_AMBIGUOUS` | 2 | `inspect --evidence NAME` names a section of several files (`details.candidates: [{path, configurations}]`) | use `--evidence path#section` or `--configuration` |
| `RECORD_NOT_FOUND` | 3 | no such record id (`details.did_you_mean`) | check the id |
| `REVISION_NOT_AVAILABLE` | 3 | the index holds another revision (`details.available_revision`) | omit `--revision` or use another index |
| `CONFIGURATION_NOT_FOUND` | 3 | no such configuration (`details.available`; null = Project as written) | use a listed id |
| `EVIDENCE_SECTION_NOT_FOUND` | 3 | `inspect --evidence` names a section (or `path#section`) missing from the configuration scope | use the sections `inspect` lists |
| `SNAPSHOT_MISMATCH` | 4 | the cursor was made on another index snapshot | rerun without the cursor |
| `CURSOR_SEMANTICS_MISMATCH` | 4 | the cursor was made by a reader with other query code or vocabulary (`details.cursor_reader`, `details.reader`) | restart the search without a cursor |
| `INDEX_UNAVAILABLE` | 5 | no usable index (`details.tried: [{source, path, problem}]`), including one built by other vocabulary or indexing code; or a damaged index whose stored Project (nothing is written) or evidence (`inspect --evidence`, nothing of it shown) fails its sha256 check | `os-optics-lens index build --corpus <dir> --out <file>`, then `--index <file>` or `OS_OPTICS_LENS_INDEX` |
| `INDEX_IN_USE` | 5 | `index build` cannot replace an index open in another process (Windows); a directory or read-only `--out` is `INVALID_REQUEST` | close readers or use another `--out` |
| `BUILD_FAILED` | 6 | the corpus has error findings (`details.findings`, errors first, at most 50; `findings_omitted`) | fix each finding's `file` and `field`; the previous index is unchanged |
| `INTERNAL` | 1 | unexpected failure (a bug) | rerun with `OS_OPTICS_LENS_DEBUG=1` and report it |

In a batch, a failing sub-query becomes `{"index", "ok": false, "error"}` in `results` and the
others still run; the command exits 0 unless every sub-query failed (then with the first
error's code).

Build findings (`index build` / `index validate`) each carry `{severity, code, record_id, file,
field, message, expected?, actual?}`; docs/INDEX_SCHEMA.md lists every finding code.

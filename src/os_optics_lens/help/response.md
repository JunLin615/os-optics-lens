# Response format

Every command prints exactly one JSON document followed by a newline. The JSON is ASCII
(non-ASCII characters are `\uXXXX` escapes), so it reads the same under every console encoding.

## Envelope

```
{"ok": true,
 "snapshot": {"id": "0262201eaf2c8e3c", "records": 41, "configurations": 41},
 "result": {...}                  single query and every other command
 "results": [...],                batch only, instead of result
 "warnings": [...],               optional: {code, pointer, message?, did_you_mean?}
 "bounds": {"max_output_bytes": 32768, "output_bytes": 1577, "truncated": false}}
```

* `snapshot.id` identifies the index content. Cursors and comparisons are valid within one
  snapshot.
* `bounds.output_bytes` is the exact length of the compact document including the final newline,
  and never exceeds `max_output_bytes`. `truncated` is true when a page stopped early to stay in
  budget or a candidate was compacted.
* Warnings do not fail the request: `UNKNOWN_FIELD` (a shorthand fact absent from every
  configuration; that constraint is UNCONFIRMED FACT_MISSING everywhere) and `FACET_VALUE_EMPTY`
  (a known term with no records in this index).

Errors: `{"ok": false, "error": {"code", "message", "hint", "details"}}` with a non-zero exit
code (`os-optics-lens help errors`).

## search result

```
{"mode": "specification",
 "total": {"evaluated": 22, "MATCH": 1, "UNCONFIRMED": 0, "DOES_NOT_MATCH": 21, "listed": 1,
           "excluded": {"filters": 19, "missing_fact": 0, "out_of_range": 0,
                        "hard_shortfall": 0, "outcome": 21}},
 "candidates": [ ... ],
 "page": {"limit": 10, "offset": 0, "returned": 1, "next_cursor": null}}
```

`total` counts records: `evaluated` after filters, per best-configuration outcome, `listed` in the
ordered list being paged; `excluded` says why the others are absent (`outcome`: not in `include`;
`hard_shortfall`: starting_point configurations with a hard DOES_NOT_MATCH).

A candidate (one record, at its best configuration):

```
{"record_id": "yutong-2025-mirrorless-18-p2b", "revision": 4, "title": "...",
 "collection": "yutong-cup", "family": "yutong-2025-mirrorless-18",
 "configuration_id": null,                     null = the Project as written
 "outcome": "MATCH",
 "counts": {"MATCH": 2, "UNCONFIRMED": 0, "DOES_NOT_MATCH": 0},
 "constraints": {"c0": {"outcome": "MATCH", "unpinned": {"_text": "Primary wavelength 587.0 nm (vacuum)."}},
                 "c1": "MATCH"},
 "shortfalls": [],
 "unknowns": [],
 "facts": {"first_order.effective_focal_length_mm": {"value": 18.726, "unit": "mm", "basis": "ENGINE_COMPUTED"}, ...},
 "rank": {"position": 1, "key": [...], "explanation": "MATCH: 2 of 2 constraints match, ..."},
 "other_configurations"?: [{"configuration_id", "outcome", "counts"}],
 "notes"?: [...],                              e.g. multi-valued range matches
 "compacted"?: true}                           budget fallback without facts/details
```

* `constraints` (compact): `"MATCH"` when the constraint pinned every condition of the matched
  fact; `{"outcome": "MATCH", "unpinned": {...}}` when it did not (up to 16 keys; prose conditions
  as `_text`); `{"outcome": "UNCONFIRMED", "reason": CODE}`; `{"outcome": "DOES_NOT_MATCH"}`.
  With `detail: "full"` each entry is the full evaluation: `outcome`, `reason`, `details`,
  `shortfalls`, `unpinned`, `fact` (json_path, value, unit, basis, conditions, source with its
  resolution status) and per-predicate results.
* `shortfalls`: hard DOES_NOT_MATCH gaps `{constraint, path, value, limit, shortfall, relative,
  strict?, unit?}` (`shortfall` and `relative` are `null` when not representable as finite numbers;
  non-numeric failures list the failing predicates), then target deviations
  `{target, fact, value, target_value, deviation, normalized, scale_basis, weight, unit}`; the
  target score is the sum of `weight * normalized`.
* `unknowns`: `{constraint | target | preference, reason}` for everything UNCONFIRMED or not
  comparable (full detail adds `details`).
* `facts` holds summary facts `{value, unit, basis}`. Compact `basis` gives the class
  (`PROJECT_DECLARED`, `PROJECT_DERIVED`, `ENGINE_COMPUTED`, `DERIVED` or `OTHER`). Full detail
  gives the verbatim basis.
  Multi-valued facts show `{values: n, min, max, unit}`. Discovery-only facts carry
  `eligible_for: "DISCOVERY_ONLY"`. Defaults: EFL, the system aperture, the field, image circle,
  total track, sampling span and every fact named by the query (at most 12); `fields` overrides.
* `rank.key` is the exact sort key. Specification: `[outcome, preference entries..., record_id,
  configuration ordinal]`. Starting point: `[hard DNM count, hard UNCONFIRMED count, unknown
  targets, weighted target score, preference entries..., record_id, ordinal]`. A preference entry
  is `[0, v]` ascending, `[0, -v]` descending, `[1, null]` unknown (ranked last).

## Pages and batches

`page.next_cursor` is null at the end; otherwise rerun the same query with that cursor. Pages
concatenate to the full ordered list and are identical for the same snapshot and query.

A batch returns `"results": [{"index": 0, "ok": true, "result": {...}}, {"index": 1, "ok": false,
"error": {...}}]`, one budget for all entries. Each page with results first gets one candidate
(compact if the full form does not fit), then pages fill round-robin in query order.

## Other commands (`result`)

* `info`: `package`, `index {path, source}`, `meta` (build metadata), `build_warnings`, `next`.
* `facets`: `{records, configurations, facets: {name: {level, values: [[value, records]...],
  more, description?}}}`.
* `fields`: `{records, fields: {key: {records, unit, value_type, bases, condition_variants,
  conditions_keys, min, max, stop_dependent}}, returned, more}`; with `--fact`: one key with
  `value_paths`, `applicable_ops`, `eligible_for`, `constraint_form`, `variants`,
  `example_constraint`.
* `compare`: `{candidates: [{ref, record_id, revision, configuration_id, title}], rows: [{fact,
  values: [cell | null], comparable, differs_by?, missing?, notes?}]}`. `differs_by` lists
  `multi_valued`, `unit`, `conditions`, `qualifiers` or `sampled_field_fractions`; when grids
  differ, each single-valued cell shows its `sampled_field_fractions` (`null` without one). Refs
  must name distinct configurations; `ID@` (empty configuration id) is `INVALID_REQUEST`.
* `inspect` returns identity, `collection {collection_id}`, `design`, `native_project`,
  `configuration`, `configurations`, `facets`, `fact_keys`, `evidence` and `next`. Use `--fields`,
  `--sections` and `--prescription` for Project details. `--evidence` returns `evidence_sections`
  keyed by path, with `sha256` and `sections {name: content}` for each file. `evidence` lists
  `refs {path, section, status, count}`, `sections {path: [names]}` and files with their
  configuration IDs. A damaged file has a `status` and lists no sections. `--configuration`
  narrows these lists to one configuration. `--provenance` adds `provenance` with the direct
  source, attribution and `license` terms when available.
* `project` returns `{record_id, revision, sha256, size_bytes, native_configuration_ids,
  configurations, engine_version, next}`. `--out` adds `path` (absolute) and `written_sha256`.
  `--stdout` prints the raw Project bytes instead of JSON (`--max-output-bytes` is rejected
  there). The stored bytes are re-checked against `sha256` before anything is written; `--out`
  replaces the target only after the written bytes hash correctly, and a path that cannot be
  written is `INVALID_REQUEST` naming it.
* `index build` / `index validate`: `{out, snapshot_id, records, configurations, facts, warnings,
  findings, findings_omitted, use}` (`out` and `use` are null for validate).
* `index dump`: `{index_schema_version, tables: {name: {columns, rows}}}` (not budgeted).

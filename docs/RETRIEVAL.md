# Retrieval: installing, querying and rebuilding the index

`os-optics-lens` is a local retriever with a bundled index. An agent narrows the collection
with facets, learns which typed quantities it can constrain, runs a specification or
starting-point search, compares and inspects a short list, and writes the chosen native
`os-optics` Project to a file. Queries run offline from the installed package. Every command
prints one bounded JSON document.

- [Installation](#installation)
- [Quickstart: from brief to editable Project](#quickstart-from-brief-to-editable-project)
- [Commands](#commands)
- [Request format](#request-format)
- [Response format](#response-format)
- [Semantics](#semantics)
- [Errors](#errors)
- [Building and validating the index from a checkout](#building-and-validating-the-index-from-a-checkout)
- [Windows and PowerShell](#windows-and-powershell)
- [Baseline timings](#baseline-timings)

The same reference ships with the package: `os-optics-lens help quickstart | request | response
| semantics | errors | facets | fields`.

## Installation

The runtime uses only the Python standard library (Python 3.10 or newer). The wheel contains the
retriever, a prebuilt SQLite index and the corpus it was built from (records, native Projects,
evidence extracts, license notices and acceptance fixtures).

The package is not on PyPI yet; the first PyPI release is a separate release step. Until then,
build the wheel from a checkout (below). After that release, `pip install os-optics-lens` installs
the same wheel.

```
pip install dist/os_optics_lens-0.1.0-py3-none-any.whl
os-optics-lens info                   # snapshot id, record count, index source "bundled"
python -m os_optics_lens info         # same CLI without the console script
```

From a checkout, build a wheel and install it, or run the sources against a locally built index:

```
uv build --wheel                                  # dist/os_optics_lens-<version>-py3-none-any.whl
pip install dist/os_optics_lens-0.1.0-py3-none-any.whl

# without installing: build an index, then point the CLI at it
PYTHONPATH=src python -m os_optics_lens index build --corpus . --out build/os-optics-lens-index.sqlite
PYTHONPATH=src python -m os_optics_lens --index build/os-optics-lens-index.sqlite info
```

An editable install and the sdist contain no bundled index. The CLI then needs `--index FILE` or
the `OS_OPTICS_LENS_INDEX` environment variable. The index is located in this order, without
fallback and without looking in the working directory: `--index FILE`, `OS_OPTICS_LENS_INDEX`,
the bundled `os_optics_lens/_data/index.sqlite`.

## Quickstart: from brief to editable Project

The example brief is a full-frame mirrorless camera lens with an 18 mm focal length, f/2 or
faster, and an image circle covering 36 x 24 mm.

**1. Facets: what is in the collection.**

```
os-optics-lens facets --facet design_type --facet image_format --facet band
```

```
{"ok":true,"snapshot":{"id":"0262201eaf2c8e3c","records":41,"configurations":41},
 "result":{"records":41,"configurations":41,"facets":{
   "design_type":{"level":"record","values":[["camera_lens",22],["microscope_objective",10],["endoscope_objective",8]],"more":0},
   "image_format":{"level":"configuration","values":[["full_frame",2]],"more":0},
   "band":{"level":"configuration","values":[["visible",41]],"more":0}}},
 "bounds":{"max_output_bytes":32768,"output_bytes":...,"truncated":false}}
```

Counts are distinct records. Filters accept aliases (`--filter image_format=ff`).

**2. Fields: what can be constrained.**

```
os-optics-lens fields --filter design_type=camera_lens --prefix aperture.
os-optics-lens fields --fact image_format.image_circle_diameter_mm
```

`fields --fact` returns the unit, value type, applicable operators, condition variants, whether
the fact is stop-dependent and a ready-to-run `example_constraint`:

```
"example_constraint":{"fact":"image_format.image_circle_diameter_mm","op":"between_inclusive","value":[43.26,43.27]}
```

**3. Search.** Hard requirements go in `constraints`:

```
os-optics-lens search --mode specification --filter design_type=camera_lens \
  --where 'first_order.effective_focal_length_mm between_inclusive [17.1,18.9]' \
  --where 'image_format.image_circle_diameter_mm gte 43.27'
```

When no record meets the brief, look for starting points: flexible `targets` are ranked by
normalized deviation, and each candidate lists its `shortfalls` and `unknowns`:

```
os-optics-lens search --mode starting_point --filter design_type=camera_lens \
  --target first_order.effective_focal_length_mm=24~3 --prefer design.powered_element_count:asc
```

**4. Compare** the short list under matching conditions:

```
os-optics-lens compare lenslibrary-528155 yutong-2025-mirrorless-18-p2b \
  --fields first_order.effective_focal_length_mm,aperture.f_number,aperture.entrance_pupil_f_number
```

A missing value is `null` with its position in `missing`. Rows whose units, conditions,
qualifiers or sampling grids differ are `comparable: false` with `differs_by`.

**5. Inspect** one candidate. Start with its Project identity, configuration, available fact keys
and evidence section names. Request the Project facts, prescription or supporting analysis you
need. Use `--provenance` for the record's direct source, attribution and redistribution terms.

```
os-optics-lens inspect yutong-2025-mirrorless-18-p2b
os-optics-lens inspect yutong-2025-mirrorless-18-p2b --fields performance.mtf_field_minimum,first_order
os-optics-lens inspect yutong-2025-mirrorless-18-p2b --evidence field_performance
os-optics-lens inspect yutong-2025-mirrorless-18-p2b --prescription summary
os-optics-lens inspect lenslibrary-528155 --provenance
```

**6. Project hand-off.**

```
os-optics-lens project yutong-2025-mirrorless-18-p2b --out work/p2b.project.json
```

```
{"ok":true,"snapshot":{...},"result":{"record_id":"yutong-2025-mirrorless-18-p2b","revision":4,
 "sha256":"14090b01fe27afcd61f8a3e111b4e8a9bdf5e726237a2f822a07a88d1176cbe2","size_bytes":75650,
 "native_configuration_ids":[],"configurations":[...],"engine_version":"0.14.0",
 "next":"...","path":"/abs/path/work/p2b.project.json",
 "written_sha256":"14090b01fe27afcd61f8a3e111b4e8a9bdf5e726237a2f822a07a88d1176cbe2"},"bounds":{...}}
```

The file holds the exact Project bytes; `written_sha256` equals the record's
`native_project.sha256`. Open, check and evaluate it with `os-optics`.

## Commands

```
os-optics-lens info
os-optics-lens facets  [--facet NAME ...] [--filter FACET=V[,V] ...] [--range FACT=MIN:MAX ...]
                       [--spectral-span LOW:HIGH] [--record-id ID ...] [--limit N] [--describe]
os-optics-lens fields  [filters] [--fact KEY] [--prefix TEXT] [--limit N]
os-optics-lens search  [REQUEST_JSON | - | --request FILE | --fixture FILE] [--explain-request]
                       quick flags: --mode, --filter, --range, --spectral-span, --record-id,
                       --where 'FACT OP VALUE', --target FACT=VALUE[~TOL|~PCT%],
                       --prefer FACT[:asc|desc], --include, --hard-shortfall
                       output: --fields K1,K2 --detail compact|full --limit N --cursor C
os-optics-lens compare REF REF ... [--fields K1,K2]          REF = record_id[@configuration_id]
os-optics-lens inspect RECORD_ID [--revision N] [--configuration ID] [--fields K1,K2]
                       [--sections S1,S2] [--evidence S|PATH#S|all] [--prescription summary|full]
                       [--provenance]
os-optics-lens project RECORD_ID [--revision N] [--out FILE [--force] | --stdout]
os-optics-lens index build    [--corpus DIR] [--out FILE] [--allow-unresolved-evidence]
os-optics-lens index validate [--corpus DIR] [--allow-unresolved-evidence]
os-optics-lens index dump     [--index FILE]
os-optics-lens help [quickstart|request|response|semantics|errors|facets|fields]

global (before or after the command): --index FILE, --pretty
budgeted commands: --max-output-bytes N (2048..262144, default 32768)
```

Every command and subcommand has `--help` with examples. Output conventions:

- One JSON document per invocation, ASCII-only (non-ASCII as `\uXXXX`), written as bytes and
  followed by one newline. It reads the same under every console encoding. `--pretty` indents
  it; `bounds` always count the compact form.
- `--help`, `help <topic>` and `project --stdout` are the only non-JSON outputs.
- Command-line mistakes are JSON too: `INVALID_REQUEST` with `details.argv_error` and
  `details.usage`, exit code 2. Out-of-range values are rejected with `details.allowed`, never
  clamped.
- Nothing is written to stderr. With `OS_OPTICS_LENS_DEBUG=1`, an `INTERNAL` error also prints
  its traceback to stderr.
- An error envelope is bounded like any other document: by the request's `max_output_bytes`,
  else `--max-output-bytes`, else 32768. Values echoed from the request are shortened and
  `BUILD_FAILED` findings beyond the budget are counted in `findings_omitted`;
  `details.truncated` is true when anything was cut.
- A reader that closes stdout early (`| head`) ends the command silently with exit code 1.

## Request format

`search` accepts one query or a batch, as JSON from `--request FILE`, from stdin (`-`) or as a
positional argument. It also accepts an acceptance fixture via `--fixture FILE`. Request files
may be UTF-8 (with or without BOM) or UTF-16/UTF-32 with a BOM. NaN and Infinity are rejected.
The schema is closed: an unknown key fails with `details.pointer` (a JSON pointer such as
`/queries/0/constraints/3/predicates/0/op`) and `did_you_mean`. A key repeated within one object
is `INVALID_REQUEST` with the pointer of the repeated member (it never silently overrides).

### Single query

```json
{
  "mode": "specification",
  "filters": {
    "facets": {"design_type": ["camera_lens"], "image_format": ["full_frame"]},
    "ranges": [{"fact": "first_order.effective_focal_length_mm", "min": 15, "max": 30}],
    "spectral_span": [450, 650]
  },
  "constraints": [
    {"id": "efl", "fact": "first_order.effective_focal_length_mm", "op": "between_inclusive", "value": [17.1, 18.9]},
    {"id": "circle", "fact": "image_format.image_circle_diameter_mm", "op": "gte", "value": 43.27},
    {"id": "fno", "fact": "aperture.entrance_pupil_f_number", "op": "lte", "value": 2.0,
     "conditions": {"convention": "efl_over_admitted_entrance_pupil_diameter"}}
  ],
  "preferences": [{"fact": "design.powered_element_count", "direction": "ascending"}],
  "include": ["MATCH", "UNCONFIRMED"],
  "fields": ["first_order.effective_focal_length_mm", "first_order.total_track_mm"],
  "detail": "compact",
  "limit": 10,
  "cursor": null,
  "max_output_bytes": 32768
}
```

| member | meaning |
|---|---|
| `mode` | required: `specification` (hard constraints decide the outcome) or `starting_point` (targets rank near matches) |
| `filters` | discovery narrowing: `facets` (OR within, AND across; aliases), `ranges` (`min` and/or `max` over a numeric fact; a record without the fact is excluded), `spectral_span` (sampling span overlaps; discovery only), `record_ids`, `collections` (both non-empty lists of strings when present; omit them for no restriction) |
| `constraints` | hard constraints; required (non-empty) in specification mode |
| `targets` | starting_point only: `{id?, fact, target, op?: "near", tolerance? \| relative_tolerance?, conditions?, select?, path?, required_sampled_field_fractions?, weight?: 1}`; the scale (`tolerance`, else `relative_tolerance * \|target\|`, else `\|target\|`) must be a normal positive number, and a normalized deviation or weighted score that is not finite is `INVALID_REQUEST` |
| `preferences` | list, applied in order: `{fact, direction: ascending\|descending, conditions?, select?, path?, required_sampled_field_fractions?}` or the fixture's cost form |
| `include` | outcomes listed: default `MATCH`, `UNCONFIRMED` (specification), all three (starting_point) |
| `hard_shortfall` | starting_point: `exclude` (default) drops configurations with a hard DOES_NOT_MATCH; `allow` keeps and ranks them |
| `evaluation_scope` | only `one_nominal_configuration` (default) |
| `fields` | summary facts per candidate (at most 12; default EFL, system aperture, field, image circle, total track, sampling span plus every fact the query names) |
| `detail` | `compact` (default) or `full` (per-constraint fact, conditions, source and resolution status) |
| `limit` | candidates per page, 1..50 (default 10) |
| `cursor` | `page.next_cursor` of the previous page |
| `max_output_bytes` | 2048..262144 (default 32768) |

Facets are listed with `os-optics-lens facets` (descriptions with `--describe`; see
`help facets`). Phone records add the record-level facet `module_role` from `design.module_role`
(`main_camera`, `ultrawide`, `telephoto`, `front_camera`; aliases `main`, `ultra_wide`, `tele`,
`front`): where the module sits in a device, not what the prescription is. A record value outside
those terms is indexed verbatim with an `UNKNOWN_TERM` build warning, and a filter matches it
exactly. A canonical term with no records gives an empty result with `FACET_VALUE_EMPTY`.

```
os-optics-lens facets --facet module_role
os-optics-lens search --mode starting_point --filter module_role=tele --prefer first_order.total_track_mm
```

### Constraints

The shorthand `{id?, fact, op, value, path?, conditions?, select?, tolerance?,
requires_blocking_stop?, required_sampled_field_fractions?, requires_evidence_source?}` is
expanded against each configuration view:

- **Object facts** (`first_order.effective_focal_length_mm`, `aperture.f_number`) compare their
  `value` member.
- **Scalar facts** (`image_format.image_circle_diameter_mm`, `design.conjugate`,
  `design.powered_element_count`) compare the scalar itself.
- **`performance.<metric>`** selects the entries with that metric and compares `value`, or the
  first `value_*` member.
- `has_source` on an object or metric fact named without a member checks its `source`, and
  `has_sourced_method` checks its `methods`, instead of the value member.
- `path` names another member, for example `"path": "min_percent"` on the interval fact
  `performance.distortion_range`. An interval without `path` is `INVALID_REQUEST`, except with
  `covers_interval`.
- `conditions` pins condition members by JSON value equality; every pinned member must be
  present in the fact (see [Performance conditions](#performance-conditions)).
- `required_sampled_field_fractions` (a non-empty list of finite numbers; `null` and `[]` are
  `INVALID_REQUEST`) pins the fact's sampling grid; targets and shorthand preferences accept it
  too (see [Sampling grids](#sampling-grids)).
- `select` narrows metric entries by top-level members before conditions are compared, for
  example `{"frequency_lp_mm": 30, "image_height_mm": 12.0, "direction": "sagittal"}`.

The fixture form `{id, scope, metric?, predicates: [{path, op, value}], conditions?, select?,
requires_*?}` of `acceptance/*.json` is accepted verbatim.

| op | value |
|---|---|
| `eq`, `ne` | any JSON value (JSON equality: `0 == 0.0`, `true != 1`, ordered lists) |
| `gt`, `gte`, `lt`, `lte` | finite number |
| `between_inclusive` | `[low, high]` |
| `in` | list of allowed values |
| `includes_all` | list of numbers that must each have an equal member; optional `tolerance` |
| `covers_interval` | `[a, b]` against an interval object, `spectrum.continuous_coverage_nm` (a list is `VALUE_TYPE`) |
| `proportional_weights` | weight vector compared by ratio (same ordered wavelengths) |
| `has_source`, `has_sourced_method` | no value: the source (or a method's source) must resolve |

### Quick flags

`search --mode MODE` builds one query from flags:

| flag | request member |
|---|---|
| `--filter FACET=V1,V2` (repeatable; repeats OR within a facet) | `filters.facets` |
| `--range FACT=MIN:MAX` (either end may be empty) | `filters.ranges` |
| `--spectral-span LOW:HIGH` | `filters.spectral_span` |
| `--record-id ID` (repeatable) | `filters.record_ids` |
| `--where 'FACT OP VALUE'` (split on the first two whitespace runs; VALUE is JSON, else a bare string) | shorthand constraint |
| `--target FACT=VALUE~TOL` / `FACT=VALUE~15%` | target with `tolerance` / `relative_tolerance` |
| `--prefer FACT[:asc\|desc]` | preference (ascending by default) |
| `--include MATCH,UNCONFIRMED` | `include` |
| `--hard-shortfall allow` | `hard_shortfall` |

Quick flags can rank a sampled value such as `performance.mtf_field_minimum` when the eligible
values share a comparable grid and conditions. To pin a specific grid, set
`required_sampled_field_fractions` in a JSON target or shorthand preference and pass
`--request FILE`.

`--fields`, `--detail`, `--limit`, `--cursor` and `--max-output-bytes` also work with a
single-query document. The flags that build a query cannot be combined with a document.
`--explain-request` prints the normalized query (aliases resolved, defaults and ids filled in,
shorthands as written) and its `query_digest` without searching. With a cursor it also checks
the cursor against the reader, the index snapshot and the query (`CURSOR_INVALID`,
`CURSOR_SEMANTICS_MISMATCH`, `SNAPSHOT_MISMATCH`, `CURSOR_MISMATCH`, as the search would) and
echoes the decoded `options.cursor_offset`. It is bounded like the search: by
`--max-output-bytes`, else the request's `max_output_bytes` (single or batch), else 32768; an
explanation that does not fit is `OUTPUT_LIMIT_EXCEEDED` (`details.required_bytes`). `--fixture FILE` runs an
acceptance fixture as `{mode: specification, filters: {record_ids: [record.id]}, constraints,
preferences: [ranking_preference], evaluation_scope, detail: full}`. The Yutong fixture's sensor
constraint checks geometric coverage. The native P2b Project uses a continuous receiver, so a
separate constraint for the competition brief's pixel pitch is `UNCONFIRMED`.

Image-format, total-track and vertex-to-sensor facts come only from the configuration's image
receiver: the single observation on the lens axis behind the last optical surface, in the
direction the Project's sources send light. An observation attached to a component is placed in
that component's frame. `width_mm` and `height_mm` are the receiver's own u and v extents, and are
derived only when the rectangle is centred on the axis. A Project without such a receiver leaves
these constraints `UNCONFIRMED`. A value paired with a `<name>_basis` marker that is not a Project
basis leaves the projection with its marker at any depth; `field_basis` and `scale_basis` are
conventions, not markers.

### Batch

```json
{"max_output_bytes": 32768,
 "queries": [
   {"mode": "starting_point", "filters": {"facets": {"design_type": ["microscope_objective"]}},
    "targets": [{"fact": "first_order.effective_focal_length_mm", "target": 4, "relative_tolerance": 0.25}],
    "limit": 5},
   {"mode": "specification", "filters": {"facets": {"design_type": ["camera_lens"]}},
    "constraints": [{"fact": "aperture.f_number", "op": "lte", "value": 2.8}], "limit": 5}
 ]}
```

A batch holds at most 20 queries. One budget covers the whole response:

- every sub-query's skeleton is reserved first, then room for each page's first candidate in
  compact form, so every page with results returns at least one candidate (upgraded to the full
  form when it fits); after that candidates are added round-robin in query order;
- a page whose compact first candidate cannot fit even with every other page empty becomes an
  `OUTPUT_LIMIT_EXCEEDED` error entry (`required_bytes` is what it needs alone), wherever it is in
  the batch; only when pages that each fit alone do not fit together do later queries give way
  first; raise `max_output_bytes` or send fewer queries;
- an error entry's echoed values are shortened (`details.truncated`), so one long input cannot
  crowd out the other entries;
- each sub-query has its own `limit`, `cursor`, `fields` and `detail`;
- a failing sub-query becomes an error entry and the others still run.

### Continuing a result list

When `page.next_cursor` is a string, the page stopped at the limit or the byte budget. Send the
same query with that cursor (here `brief.json` holds a single query, such as the example above):

```
os-optics-lens search --request brief.json --limit 15
# ... "page":{"limit":15,"offset":0,"returned":15,"next_cursor":"eyJvIjoxNSwicSI6IjhlZTA5..."}
os-optics-lens search --request brief.json --limit 15 --cursor eyJvIjoxNSwicSI6IjhlZTA5...
```

In a batch, put each sub-query's cursor into that sub-query. A cursor binds to the reader's query
code and vocabulary, to the index snapshot and to the query's mode, filters, constraints, targets, preferences, include,
hard_shortfall and evaluation scope. `limit`, `fields` and `detail` may change between pages.
For the same snapshot and query, the pages are identical, and concatenating them gives the
full ordered list. After an upgrade that changes the query code or the vocabulary, an older cursor
is `CURSOR_SEMANTICS_MISMATCH` (a version 1 cursor is `CURSOR_INVALID`): restart the search without
a cursor. See [Reader compatibility](INDEX_SCHEMA.md#reader-compatibility).

## Response format

```
{"ok": true,
 "snapshot": {"id": "<16 hex>", "records": N, "configurations": N},
 "result": {...} | "results": [{"index", "ok", "result" | "error"}],
 "warnings"?: [{"code", "pointer", "message"?, "did_you_mean"?}],
 "bounds": {"max_output_bytes", "output_bytes", "truncated"}}
```

- `bounds.output_bytes` is the exact byte length of the document, newline included. It never
  exceeds `max_output_bytes`.
- `truncated` is true when a page stopped early for the budget or a candidate was compacted.
- Warnings do not fail the request:
  - `UNKNOWN_FIELD`: a shorthand constraint's fact is in no configuration; it is UNCONFIRMED
    FACT_MISSING everywhere.
  - `FACET_VALUE_EMPTY`: a known term has no records in this index.

### Search result (single query, compact)

```json
{"mode": "specification",
 "total": {"evaluated": 22, "MATCH": 1, "UNCONFIRMED": 0, "DOES_NOT_MATCH": 21, "listed": 1,
           "excluded": {"filters": 19, "missing_fact": 0, "out_of_range": 0, "hard_shortfall": 0, "outcome": 21}},
 "candidates": [
  {"record_id": "yutong-2025-mirrorless-18-p2b", "revision": 4,
   "title": "18 mm f/2 full-frame mirrorless P2b", "collection": "yutong-cup",
   "family": "yutong-2025-mirrorless-18", "configuration_id": null, "outcome": "MATCH",
   "counts": {"MATCH": 2, "UNCONFIRMED": 0, "DOES_NOT_MATCH": 0},
   "constraints": {"c0": {"outcome": "MATCH", "unpinned": {"_text": "Primary wavelength 587.0 nm (vacuum)."}},
                   "c1": "MATCH"},
   "shortfalls": [], "unknowns": [],
   "facts": {"first_order.effective_focal_length_mm": {"value": 18.726068736671145, "unit": "mm", "basis": "ENGINE_COMPUTED"},
             "image_format.image_circle_diameter_mm": {"value": 43.27, "unit": "mm", "basis": null},
             "spectrum.sampling_span_nm": {"value": [486, 656], "unit": "nm", "basis": "OTHER", "eligible_for": "DISCOVERY_ONLY"}},
   "rank": {"position": 1, "key": ["MATCH", "yutong-2025-mirrorless-18-p2b", 0],
            "explanation": "MATCH: 2 of 2 constraints match, 0 unconfirmed, 0 do not match."}}],
 "page": {"limit": 10, "offset": 0, "returned": 1, "next_cursor": null}}
```

**`total`** counts records:

- `evaluated`: records that passed the filters;
- one count per outcome of each record's best configuration;
- `listed`: the ordered list being paged;
- `excluded`: why the other records are absent (filters, a missing ranged fact, out of range,
  a hard shortfall in starting_point, or an outcome not in `include`).

**Candidate members:**

- **`constraints`** in compact detail:
  - `"MATCH"`: the constraint pinned every structured condition of the matched fact, and the
    fact has no prose conditions.
  - `{"outcome": "MATCH", "unpinned": {...}}`: up to 16 unpinned keys; prose conditions appear as
    `_text`.
  - `{"outcome": "UNCONFIRMED", "reason": CODE}`.
  - `{"outcome": "DOES_NOT_MATCH"}`.

  In full detail each entry is the full evaluation, including `fact {json_path, value, unit,
  basis, conditions, source {path, section, status}}` and per-predicate results.
- **`shortfalls`**:
  - hard DOES_NOT_MATCH gaps: `{constraint, path, value, limit, shortfall, relative, strict?,
    unit?}`; `shortfall` and `relative` are `null` when not representable as finite numbers; a
    non-numeric failure lists its failing predicates instead;
  - target deviations: `{target, fact, value, target_value, deviation, normalized, scale_basis,
    weight, unit}`; the target score is the sum of `weight * normalized`.
- **`unknowns`**: `{constraint | target | preference, reason}`.
- **`facts`**: `{value, unit, basis}`.
  - Compact `basis` gives the class (`PROJECT_DECLARED`, `PROJECT_DERIVED`, `ENGINE_COMPUTED`,
    `DERIVED` or `OTHER`). `detail: full` gives the verbatim basis.
  - A multi-valued fact appears as `{values: n, min, max, unit}`.
- **`rank.key`** is the exact sort key:
  - specification: `[outcome, preference entries..., record_id, configuration ordinal]`;
  - starting_point: `[hard DNM, hard UNCONFIRMED, unknown targets, round(sum(weight *
    normalized), 9), preference entries..., record_id, ordinal]`.

  A preference entry is `[0, v]` ascending, `[0, -v]` descending, or `[1, null]` when unknown,
  which ranks last. `rank.explanation` restates the key in English.
- **`other_configurations`** lists a multi-configuration record's other configurations with
  their outcome counts.
- **`compacted: true`** marks the budget fallback form: no summary facts or details, used when a
  page's first candidate would not fit otherwise.

### Batch result

```
{"ok": true, "snapshot": {...},
 "results": [{"index": 0, "ok": true, "result": {"mode": "starting_point", ..., "page": {"limit": 5, "offset": 0,
               "returned": 1, "next_cursor": "eyJvIjoxLCJxIjoiOGVlMDlmMTYxZjAwYmVjYyIsInIiOiJlOGM1NWQ3NWQzYWQ1YjE4IiwicyI6IjAyNjIyMDFlYWYyYzhlM2MiLCJ2IjoyfQ"}}},
             {"index": 1, "ok": true, "result": {...}},
             {"index": 2, "ok": false, "error": {"code": "INVALID_REQUEST", "message": "...", "hint": "...",
               "details": {"pointer": "/queries/2/mode", ...}}}],
 "bounds": {"max_output_bytes": 4096, "output_bytes": 3479, "truncated": true}}
```

`ok` is false, and the exit code non-zero, only when every sub-query failed.

### Other results

| command | `result` |
|---|---|
| `info` | `package`, `index {path, source}`, `meta` (build metadata), `build_warnings`, `commands`, `next` |
| `facets` | `{records, configurations, facets: {name: {level, values: [[value, records], ...], more, description?}}}` |
| `fields` | `{records, fields: {key: {records, unit, value_type, bases, condition_variants, conditions_keys, min, max, stop_dependent}}, returned, more}` |
| `fields --fact` | the key's summary plus `value_paths`, `applicable_ops`, `eligible_for`, `constraint_form`, `variants` (at most 10, `more_variants`), `example_constraint` or `example_filter` |
| `compare` | `{candidates: [{ref, record_id, revision, configuration_id, title}], rows: [{fact, values: [cell \| null], comparable, differs_by?, missing?, notes?}]}`; `differs_by` may name `sampled_field_fractions`, and then each cell shows its grid (`null` without one); refs must name distinct configurations (`ID` and `ID@<its default configuration>` are the same), and an empty id after `@` is `INVALID_REQUEST` |
| `inspect` | Identity, `collection {collection_id}`, `design`, `native_project`, `configuration`, `configurations`, `facets`, `fact_keys`, supporting `evidence {refs, sections, files}` and `next`. Request Project details with `--fields`, `--sections` or `--prescription`. `--evidence` adds `evidence_sections {path: {sha256, sections}}`. `--provenance` adds `provenance` with the direct source, attribution and license terms. |
| `project` | `{record_id, revision, sha256, size_bytes, native_configuration_ids, configurations, engine_version, next}`. `--out` adds `path` (absolute) and `written_sha256`. The CLI checks the stored bytes against `sha256` before writing. It writes a temporary file and replaces the target only after the file hashes correctly. An unwritable path returns `INVALID_REQUEST`. `--max-output-bytes` is validated before any write and is rejected with `--stdout`. |
| `index build` / `validate` | `{out, snapshot_id, records, configurations, facts, warnings, findings, findings_omitted, use}`; `out` and `use` are null for validate |
| `index dump` | `{index_schema_version, tables: {name: {columns, rows}}}`; not budgeted |

## Semantics

### Outcomes

- **MATCH**: a comparable value under the requested conditions satisfies every predicate, and
  its evidence resolves when evidence is required.
- **DOES_NOT_MATCH**: a comparable value violates a predicate. A numeric violation reports
  `shortfall` (absolute distance to the violated limit, in the fact's unit) and `relative`
  (shortfall / |limit|). A strict `gt`/`lt` at equality has shortfall 0.0 and `strict: true`.
  Either number is `null` when it is not representable as a finite number, for example when a
  gap is divided by a limit of 1e-308; the outcome is unaffected.
- **UNCONFIRMED**: the record can neither confirm nor refute the constraint, and `reason` says
  why. An unknown is never a match and never a failure.

A configuration's outcome is DOES_NOT_MATCH if any constraint does not match, else UNCONFIRMED
if any is unconfirmed, else MATCH. A record is shown at its best configuration: MATCH before
UNCONFIRMED before DOES_NOT_MATCH, then more MATCH constraints, then fewer DOES_NOT_MATCH, then
(specification) the preferences, then the configuration order. `rank.key` then orders the records.

Specification search answers "does this record meet the brief as stated?" Starting-point search
answers "which records are good places to start refining?" and keeps fixed requirements,
flexible targets, shortfalls and unknowns apart.

### Reason codes

| reason | meaning |
|---|---|
| `FACT_MISSING` | the configuration has no such fact. `details.available_alternatives` lists related facts, which are never substituted |
| `CONDITIONS_DIFFER` | no entry has the requested conditions. `details` shows `expected` vs `actual` for the nearest entry, or `present: false` for a member it does not state (spectrum members as `spectrum.<member>`) |
| `AMBIGUOUS_FACTS` | several entries match. Pin more conditions or use `select` |
| `DISCOVERY_ONLY` | the fact, or an object along or under the compared path, is discovery-only (e.g. `spectrum.sampling_span_nm`) |
| `COMPARISON_ONLY` | the request explicitly names a [historical source comparison](#provenance-checks) |
| `COVERAGE_UNCONFIRMED` | `spectrum.continuous_coverage_nm` is not CONFIRMED or SOURCE_STATED |
| `SAMPLING_DIFFERS` | `required_sampled_field_fractions` differs from the fact's `sampled_field_fractions`, or the fact states none |
| `SAMPLING_UNSPECIFIED` | eligible values for an unpinned target or preference have different grids |
| `STOP_NOT_ELIGIBLE` | a stop-dependent fact on a Project whose stop does not block light |
| `EVIDENCE_MISSING` | evidence is required and the fact (or every method) has no source |
| `EVIDENCE_UNRESOLVED` | a source exists but does not resolve in the index (file, sha256 or section) |
| `VALUE_MISSING` | the value at the predicate path is null or absent |
| `VALUE_TYPE` | the value's type does not fit the operator |
| `PREFERENCE_NOT_COMPARABLE` | a preference value exists only under other conditions (e.g. other cost weights) |

### Stop gates

Stop-dependent facts are listed in `vocabulary.json`:

- eligible `aperture.*` facts other than `stop_opening_diameter_mm`;
- `performance.mtf`, `performance.mtf_field_minimum` and `performance.relative_illumination`.

A new aperture member is stop-dependent by default. A predicate that compares a whole object or
list (path `""` on the `aperture` section, or the whole `performance` list) reaches every
stop-dependent fact inside it. For a predicate that reaches a stop-dependent fact, or a constraint
with `requires_blocking_stop: true`:

The configuration must have a blocking stop. Otherwise the result is `UNCONFIRMED` with reason
`STOP_NOT_ELIGIBLE`. When the stop is eligible, evaluate the requested fact with its own
conditions and evidence requirements. Targets and preferences use the same eligibility check.
`fields` reports `stop_dependent` per key, and the `stop_state` facet shows the Project stop state.

### Coverage versus discovery span

- `spectrum.sampling_span_nm` is the span of the sampled design wavelengths. The `band` facet and
  the `spectral_span` filter use it, and it is marked `DISCOVERY_ONLY`. A hard constraint on it,
  in any form, is UNCONFIRMED `DISCOVERY_ONLY`.
- `spectrum.continuous_coverage_nm` is established coverage. `covers_interval [a, b]` matches
  only when its state is CONFIRMED and it contains [a, b]; a source-stated coverage never confirms it. Otherwise the result
  is `COVERAGE_UNCONFIRMED`, with the sampling span shown for information.
- Sampled wavelengths are compared with `includes_all`/`eq` on `spectrum.sampling_wavelengths_nm`.
  Spectral weights are compared with `proportional_weights`.
- `covers_interval` needs an interval object (one `min_`/`max_` member pair). A list, such as two
  sampled wavelengths `[532, 1064]`, says nothing about the gap between them and is UNCONFIRMED
  `VALUE_TYPE`.

### Provenance checks

`inspect ID --provenance` returns the direct source in `result.provenance`. Depending on the
record, this includes the source name, input identifier, pinned source revision, source links,
contributor, attribution and license terms. Use `inspect ID --evidence SECTION` to read the
supporting calculations and analyses for the Project.

Historical comparisons such as `first_order.engine_check`, `field.prescription_dump_check` and
`materials.catalog_comparisons` remain contribution review data. A constraint explicitly naming
one returns `UNCONFIRMED` with `COMPARISON_ONLY`. A target, preference, range or field naming one
returns `UNKNOWN_FIELD` with `details.provenance_check`. Whole-section requests use the projected
Project facts. See the [historical check fields](RECORD_FORMAT.md#provenance-checks).

### Aperture conventions

Use the Project fact for the aperture convention in your brief.

- an image-space f-number: `aperture.f_number`, `aperture.image_space_f_number`;
- a paraxial working f-number: `aperture.paraxial_working_f_number`;
- a ray-traced working f-number: `aperture.working_f_number`;
- an entrance-pupil f-number from EFL and the admitted pupil: `aperture.entrance_pupil_f_number`,
  with a `convention` condition;
- an entrance pupil diameter;
- an object-side numerical aperture;
- a stop opening diameter.

The `system_aperture` facet names the aperture the design file declares. Constrain the quantity
the brief means. A record without it is FACT_MISSING, and `available_alternatives` shows which
aperture facts it has.

### Back-focus endpoints

- `first_order.back_focal_length_mm` is the paraxial distance from the last surface vertex to the
  paraxial image point.
- A geometric back focus to a fixed image plane is a separate fact, for example
  `first_order.final_powered_vertex_to_sensor_mm`. Its conditions are `distance_kind:
  geometric_axial_distance`, `from: last_powered_surface_vertex`, `to: fixed_image_plane` and
  `includes_cover_glass`, together with `image_format.cover_glass`.
- Pin the endpoints your brief means. Different endpoints are CONDITIONS_DIFFER, never a
  substitute.

### Performance conditions

Performance entries carry structured `conditions`: spatial frequency, field coordinate and
bounds, spectrum, image plane, orientation and so on.

- A constraint compares only the entries whose conditions equal every key it pins. Equality is
  JSON value equality: `0 == 0.0`, a boolean never equals a number, lists are ordered and
  objects compare key-wise.
- A pinned key must be **present** in the fact's `conditions`. An absent key never equals a
  requested `null`; a stored explicit `null` does.
- A `spectrum` object is compared member by member: every requested member must be present.
  `incident_weights` compare by ratio, `sampling_wavelengths_nm` as an ordered list and every
  other member (such as `wavelength_medium`) by JSON equality. Weights are meaningless without
  their ordered wavelengths, so a request giving `incident_weights` without
  `sampling_wavelengths_nm` is `INVALID_REQUEST`.
- The same presence rule applies to `select`, target and preference `conditions`, and the cost
  pins of the fixture preference form.
- The reference evaluator in `tests/test_yutong_acceptance.py` treats an absent condition like
  `null` and compares only a spectrum's wavelengths and weights. The retriever is deliberately
  stricter in both cases; the reference file is kept unchanged.
- Keys it does not pin are reported as `unpinned` on a MATCH, including the unrequested members
  of a partly pinned spectrum (`spectrum.incident_weights`) and an unpinned sampling grid
  (`sampled_field_fractions`).
- Prose conditions (`conditions_text`) are information only.
- `select` narrows entries by their top-level members, for example a frequency, image height and
  direction for a field-sampled MTF.
- `requires_evidence_source` requires the source to resolve.

### Sampling grids

An interval MTF is a minimum over its sampled field positions (`sampled_field_fractions`). The
ordered grid is part of the comparison because changing the sample positions can change the
minimum.

- `required_sampled_field_fractions` pins an exact grid in a constraint, target or shorthand
  preference. A different or missing grid yields `SAMPLING_DIFFERS`.
- A sampled target or preference can rank eligible configurations that share a grid, units,
  structured conditions and performance qualifiers. Ordered spectra with proportional incident
  weights can compare under the same conditions. Different unpinned grids yield
  `SAMPLING_UNSPECIFIED`, including a mix of sampled and unsampled values. Differences in other
  comparison conditions on a shared grid yield `CONDITIONS_DIFFER`. Search reports these reasons
  in `unknowns` and omits the affected term from ranking.
- `fields --fact` shows condition variants and their grids. The example constraint for a sampled
  fact includes `required_sampled_field_fractions`.
- `compare` marks rows with different grids `comparable: false`, lists `sampled_field_fractions`
  in `differs_by` and shows each cell's grid.

### Evidence

A fact's `source {path, sha256, section}` resolves when the index holds that file with that
SHA-256 over LF bytes and `sections[section]` exists. Resolution reads only the index and never
the file system, so an installed package answers the same as a checkout. The files follow one
[evidence extract contract](RECORD_FORMAT.md#evidence-extracts): only members of `sections` are
addressable, and a record's `collection.evidence_extract` must carry that record's `record_id`.
In an index built with `--allow-unresolved-evidence`, a rejected `collection.evidence_extract`
contributes no file or section to `inspect`, even if another record's reference stored the file.

Every use of evidence **verifies the bytes**: the stored content is hashed again and must match
both the stored digest and the one the reference expects. So every reported status is live:

- constraints that require the evidence are `UNCONFIRMED` (`EVIDENCE_UNRESOLVED`) when the bytes
  do not verify;
- the default `inspect` shows the live status in `evidence.refs`, marks the file in
  `evidence.files` with `"status": "HASH_MISMATCH"` (or `FILE_MISSING`) and lists no sections for
  it;
- `inspect --evidence` of a record with such a file fails with `INDEX_UNAVAILABLE` and shows none
  of its bytes (the damaged file's sections are unknown, so no selection over that record is safe).
  Rebuild the index.

`inspect` scopes evidence to configurations. The **scope** is the `--configuration` named, else
every configuration of the record. A configuration references a (file, section) pair when a source
in its view names it.

- A pair referenced only by configurations outside the scope does not apply.
- The other sections of the files that the scope references, and of the record-level
  `collection.evidence_extract` unless only outside configurations reference that file, apply. A
  record with only an `evidence_extract` therefore resolves its sections.
- The default output lists `evidence.refs`, `evidence.sections {path: [names]}` and
  `evidence.files {path: {configurations, status?}}` (the configurations referencing each file;
  `status` only for a damaged one), all
  scoped.
- `--evidence NAME` returns the one applicable file's section. Several distinct files with that
  section are `EVIDENCE_AMBIGUOUS` (`details.candidates: [{path, configurations}]`); select one
  as `--evidence path#section` or narrow with `--configuration`.
- `--evidence all` returns every applicable pair. `evidence_sections` is keyed by path, so equally
  named sections of different files are all kept.

### Configurations

- A record whose native Project declares configurations is indexed per configuration, with the
  native `configuration_id`, and all hard constraints are evaluated within one configuration.
- A record never MATCHes because different configurations satisfy different constraints.
- A record without declared configurations has one configuration, the Project as written, with
  `configuration_id: null`. A Project that declares exactly one configuration gives it that id.
- `inspect --configuration ID` and `compare record_id@ID` select a configuration.
- See [RECORD_FORMAT.md](RECORD_FORMAT.md) (record schema version 1.1).

### Bases and multi-valued facts

- **Bases.** `PROJECT_DECLARED` identifies a native Project value. `PROJECT_DERIVED` identifies
  a value computed from Project data. `ENGINE_COMPUTED` identifies a calculation by `os-optics`.
  Some applicable analyses retain a legacy `DERIVED` basis.
- **Multi-valued facts.** A fact key with several entries in one configuration, such as
  `performance.mtf` sampled at several frequencies and heights, is multi-valued.
  - Its summary is `{values, min, max, unit}`.
  - A range filter matches when any value is in range.
  - A constraint must narrow to one entry, else the result is AMBIGUOUS_FACTS.

## Errors

A failure prints `{"ok": false, "error": {"code", "message", "hint", "details"}}` and exits non-zero.

| code | exit | meaning |
|---|---|---|
| `INVALID_REQUEST` | 2 | malformed JSON (including a number beyond the double range, a lone surrogate escape, nesting deeper than 128 or a repeated key), unknown key (`pointer`, `did_you_mean`), bad value, out-of-range option (`allowed`), command-line error (`argv_error`, `usage`), a `project --out` or `index build --out` path that cannot be written (`path`, `os_error`) |
| `UNKNOWN_FACET` | 2 | no such facet |
| `UNKNOWN_FACET_VALUE` | 2 | a value that is neither a vocabulary term nor in the index (close values suggested) |
| `AMBIGUOUS_TERM` | 2 | an alias with several meanings (`details.candidates`) |
| `UNKNOWN_FIELD` | 2 | a fact key not in the index, used in ranges, targets, preferences, `fields --fact` or compare/inspect fields (`did_you_mean`), or a provenance check name (`provenance_check`) |
| `UNSUPPORTED_OPERATOR` | 2 | unknown `op` (`supported` lists the operators) |
| `CURSOR_INVALID` | 2 | not a cursor, or a version 1 cursor (restart without it) |
| `CURSOR_MISMATCH` | 2 | cursor of another query |
| `OUTPUT_LIMIT_EXCEEDED` | 2 | the response cannot fit `max_output_bytes` (`required_bytes`) |
| `FILE_EXISTS` | 2 | `project --out` target exists; add `--force` |
| `EVIDENCE_AMBIGUOUS` | 2 | `inspect --evidence NAME` names a section of several files (`section`, `candidates: [{path, configurations}]`) |
| `RECORD_NOT_FOUND` | 3 | unknown record id (`did_you_mean`) |
| `REVISION_NOT_AVAILABLE` | 3 | the index holds another revision (`available_revision`) |
| `CONFIGURATION_NOT_FOUND` | 3 | unknown configuration (`available`) |
| `EVIDENCE_SECTION_NOT_FOUND` | 3 | `inspect --evidence` names a section the configuration scope does not have (`available`) |
| `SNAPSHOT_MISMATCH` | 4 | cursor from another index snapshot; rerun without it |
| `CURSOR_SEMANTICS_MISMATCH` | 4 | cursor from a reader with other query code or vocabulary (`cursor_reader`, `reader`); restart without it |
| `INDEX_UNAVAILABLE` | 5 | no usable index (`tried: [{source, path, problem}]`), including one built by other vocabulary or indexing code; or a damaged index whose stored Project (nothing is written) or evidence (`inspect --evidence`; nothing of it is shown) fails its sha256 check; rebuild |
| `INDEX_IN_USE` | 5 | `index build` cannot replace an index another process holds open (Windows); a directory or read-only `--out` is `INVALID_REQUEST` instead |
| `BUILD_FAILED` | 6 | error findings: `details.findings`, errors first, at most 50; the rest are counted in `findings_omitted` |
| `INTERNAL` | 1 | unexpected failure; rerun with `OS_OPTICS_LENS_DEBUG=1` for a traceback on stderr |

## Building and validating the index from a checkout

The committed records are the source of truth. The index is a derived file and is never committed.
[INDEX_SCHEMA.md](INDEX_SCHEMA.md) documents the tables, the findings and the reproducibility rules.

```
os-optics-lens index validate --corpus .                     # full build into a temp file, discarded
os-optics-lens index build --corpus . --out build/os-optics-lens-index.sqlite
os-optics-lens --index build/os-optics-lens-index.sqlite info
os-optics-lens index dump --index build/os-optics-lens-index.sqlite > dump.json
# from a checkout without installing, prefix with: PYTHONPATH=src python -m os_optics_lens
```

- `--corpus` is the directory that contains `records/` (default `.`). `--out` defaults to
  `build/os-optics-lens-index.sqlite`.
- A build succeeds with `{out, snapshot_id, records, configurations, facts, warnings, findings,
  use: "--index <out>"}`. `findings` lists the warnings: up to 50, with `findings_omitted`
  counting the rest.
- Any error finding fails the build: `BUILD_FAILED`, exit code 6. Each finding names its
  `file`, `field` and `code`. The previous `--out` file stays byte-identical, because the build
  writes a temporary file and replaces the target only on success.
- `--allow-unresolved-evidence` downgrades broken evidence references to warnings. Those
  references are stored as unresolved, so constraints that need them become UNCONFIRMED. Use it
  to explore a work-in-progress corpus, never to accept a batch.
- `index dump` prints the canonical content of every table. Two indexes with the same
  `snapshot_id` and dump hold the same content, across platforms and Python versions.
- A reader opens only an index built by the same vocabulary and index-producing code
  ([Reader compatibility](INDEX_SCHEMA.md#reader-compatibility)); rebuild after upgrading.
- Before merging an intake batch whose records live on other branches, verify the combined corpus
  through the CLI:

  ```
  python scripts/verify_combined_intake.py --repo <clone> --phone-modules <ref> --phone-telephoto <ref>
  ```

  It assembles this tree's records with the two phone collections taken byte-exactly from the refs,
  runs a strict validate and build, checks the `module_role` facet counts and role searches,
  evidence inspection of every phone record and the Yutong fixture, and prints a JSON report with
  the exact commits (exit 1 on a failed check).
- An installed wheel can rebuild its own index from the bundled corpus and gets the same
  snapshot id:
  `os-optics-lens index build --corpus <site-packages>/os_optics_lens/_data/corpus --out rebuilt.sqlite`.

## Windows and PowerShell

- **Pass requests as files.** PowerShell (and cmd) strip the double quotes inside a positional
  JSON argument, so `{"mode": ...}` arrives as `{mode: ...}`. The CLI detects this and says so
  in the hint. Write the request to a file and use `--request FILE`, or pipe it to `search -`.
  Quick flags (`--where`, `--target`, ...) need no JSON quoting except inside `--where` values.
- **Encoding.** Request files saved by PowerShell 5.1 (`Out-File`, `>`) are UTF-16 LE with a
  BOM. They are accepted, as are UTF-8 files with or without a BOM.
- **Project bytes.** Use `project --out FILE`. In PowerShell 5.1, redirecting `project --stdout
  > file` re-encodes the bytes (UTF-16, CRLF) and the file no longer matches the Project's
  SHA-256. `--out` writes the exact bytes and reports `written_sha256`.
- **Console encoding.** JSON output is ASCII, so any console code page (cp936, cp1252, ...)
  shows it unchanged.
- **Rebuilding.** `index build` cannot replace an index file another process still has open.
  The result is `INDEX_IN_USE` and the old file is kept. Close the reader or choose another
  `--out`.

## Baseline timings

Measured on 2026-11-26 on the development machine (Windows 10, CPython 3.13.5, SQLite 3.45.3) with
the 41-record corpus (41 configurations, 1103 facts, index 2.1 MB, snapshot `0262201eaf2c8e3c`).
Library figures are the median of repeated in-process calls on a freshly opened index; command
figures are the median wall time of a whole `os-optics-lens` process, most of which is interpreter
start-up and module import.

| Operation | In-process | Whole command |
|---|---|---|
| `index build` (41 records) | 0.20 s | 0.42 s |
| `facets` | 3 ms | 0.18-0.25 s |
| `fields` | 12 ms | 0.25 s |
| `search --fixture` (specification, 16 constraints, `detail: full`, 11 KB out) | 4 ms | 0.19-0.25 s |
| starting-point search over all records (EFL target, image-circle constraint, 15 KB out) | 12 ms | 0.26 s |
| batch of 4 queries over all records (64 KB budget) | 25 ms | - |
| `inspect` (P2b) | 3 ms | 0.19-0.25 s |
| `compare` (3 records) | 2 ms | - |
| `project --out` | - | 0.25 s |

The ranges in the last column are an installed wheel with cached bytecode (lower) and a source
checkout run with `PYTHONDONTWRITEBYTECODE=1` (upper). Search time grows linearly with the number
of configurations that pass the discovery filters; narrow with `filters` first on a large corpus.

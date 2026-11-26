# Record layout and field semantics

This document fixes the layout the first collection uses and states what every indexed value
means. [ARCHITECTURE.md](../ARCHITECTURE.md) sets the design; this file is the implementation of
it for the seed batch. Field names, units and bases are part of the record contract: change them
only with a new `record_schema_version`.

## One directory per record

```
records/lenslibrary/
├── batch-lenslibrary-seed-1.json          batch outcome manifest (one row per source input)
└── lenslibrary-528155/
    ├── project.json                       the native os-optics Project (authoritative)
    └── record.json                        compact search facts and provenance
```

`project.json` is the prescription. Everything else is discovery, comparison and evidence. A
record is independently maintainable: its directory holds no shared state, and the index is
generated from these files rather than stored beside them.

Raw ZMX files, intermediate CSV surface tables, patent PDFs and layout images stay upstream or in
a scratch workspace. They are cited by path and SHA-256, never committed.

The [camera evidence extension](CAMERA_SEARCH_EVIDENCE.md) uses this shared
format for native camera designs and provides an executable acceptance case.
Camera records may carry verified real image heights, geometric back-focus
endpoints, field-interval MTF, radiometry and sourced analysis evidence.

## Record identity

| Field | Meaning |
|---|---|
| `record_id` | `lenslibrary-<source stem>`, lower-cased. Derived from the upstream file name, so a re-import of the same input keeps the identity. |
| `record_revision` | Integer, starts at 1. Bumped when the Project or a stated fact changes for the same source input. |
| `record_hash` | SHA-256 over the record with `record_hash` and `record_hash_method` removed, so a consumer can detect an edited record. Recompute it from the **parsed JSON**, not the file's bytes: `json.dumps(body, sort_keys=True, ensure_ascii=False)` encoded as UTF-8, no trailing newline. The method is stated in the record itself. |
| `record_hash_method` | The serialization above, in prose, so the hash is reproducible without reading this document. |
| `native_project.sha256` | SHA-256 of the committed native Project bytes. Normalize checkout line endings to the repository's LF form when recomputing from a Windows worktree. |
| `collection.source_revision` | The pinned upstream commit. A record is only comparable with another record of the same revision. |
| `collection.source_input_sha256` | SHA-256 of the file's bytes **as the pinned revision stores them**, taken from the git blob, so a Windows CRLF checkout does not change it. |
| `collection.source_links.source_file_url` | The upstream ZMX at the pinned revision, so a reviewer reads the exact input. |
| `collection.original_publication` | The patent publication the source bundles with the design, when it does. `null` when the source gives none. |
| `design.source_reference` | The upstream's own short reference (`Goerz1894`, `Kidney2004`, …), preserved verbatim. |

`design.design_family_id` groups meaningful variants: `1998704a`/`b` and `7301707`/`7301707-spherical`
are one family, and separate records. Family membership is a facet, not a merge.

## Value bases

Every quantity carries a `basis`. The vocabulary is fixed:

| Basis | Meaning |
|---|---|
| `SOURCE_STATED` | Written in the upstream source: the ZMX file, the upstream prescription dump, or the LensLibrary property list. |
| `DERIVED` | Computed from source values by the import (for example a band from sampling wavelengths, or a half field from a full field). |
| `ENGINE_COMPUTED` | Produced by `os-optics` from the committed Project. |

A missing value is `null` with the field left out, never a guess. An unknown value leaves the
corresponding constraint unconfirmed.

## Spectrum

- `sampling_wavelengths_nm` — the wavelengths the source enables, as **vacuum** nanometres. The
  upstream files state air wavelengths at 20 °C and 101325 Pa; each was converted once with the
  **engine's own** air model (`material_physics.air_index_exact`, the 6432.8 / 2949810 / 25540
  form), so a record's vacuum wavelength is the wavelength the engine evaluates.
  `sampling_wavelengths_air_nm` keeps the source's own numbers so the conversion stays checkable, and
  `sampling_wavelengths_basis` states that conversion in prose.
  Using a different air model (Edlén 1953, the 8342.13 form) shifts the value by about 0.003 nm and
  is wrong here.
- `primary_wavelength_nm` — the source's primary wavelength (Zemax `PWAV`), vacuum.
- `wavelength_precision` — states that these are computational values at full precision, so a
  reviewer may pass them to the engine unchanged; rounding in this document or in prose is for
  display only.
- `sampling_span_nm` — the numeric span of the sampling wavelengths, **eligible for discovery only**.
  It is not a coverage claim and not an operating range. Two `basis` values occur:
  - `SPAN_OF_STATED_SAMPLING_WAVELENGTHS` — the min and max of several enabled wavelengths. The span
    is a summary of the samples; the samples themselves are `sampling_wavelengths_nm`.
  - `SINGLE_WAVELENGTH_BAND` — **ten records** enable exactly one wavelength. A zero-width interval
    cannot be filtered or evaluated, so the span is a **±0.5 nm window around that one sample**
    (1 nm wide). It is a numerical convenience, not a second wavelength: `sampling_wavelengths_nm`
    still holds exactly one value, and the window must never be read as two observed samples or as
    a measured band. Only `sampling_wavelengths_nm` is observed.
- `continuous_coverage_nm` — **the only interval that can satisfy a hard band constraint.** Its
  `state` is `UNCONFIRMED` with null bounds unless the source states a range independently; nothing
  in this collection does. `sampling_span_nm` never substitutes for it: a query with a hard coverage
  requirement is unconfirmed against a record whose `continuous_coverage_nm` is `UNCONFIRMED`.
- `band_facets` — `visible`, `near_infrared`, `ultraviolet`, `extreme_ultraviolet`,
  `mid_wave_infrared`, `long_wave_infrared`, from the documented boundaries in the collection
  README. A facet is a discovery label derived from the sampling span; it decides nothing about
  coverage.

## First-order properties

- `effective_focal_length_mm` — signed, in air. Its `conditions` is **prose** naming the wavelength;
  the machine-readable wavelength is `engine_check.wavelength_nm` (below), which is the number to
  pass to the engine.
- `back_focal_length_mm` — from the last surface vertex to the paraxial image point, positive
  downstream.
- `total_track_mm` — first surface vertex to the image plane along the axis.
- `engine_check` — the same two quantities recomputed from the committed Project by the engine's
  thick-lens paraxial matrix, plus `comparison` and the typed `wavelength_nm` used. This is the
  optical verification of the record, separate from conversion and from opening the Project.

### The paraxial comparison rule

A back focal length can pass through zero, so a tolerance relative to BFL alone is meaningless
there: `lenslibrary-4037934a` has a BFL of −0.0059 mm, and a 1 × 10⁻⁴ relative rule would demand
6 × 10⁻⁷ mm agreement. The scale of a first-order comparison is the system's focal length, but a
long back focus behind a short focal length needs its own length too. The rule is therefore:

```
per quantity:  |engine − source| ≤ atol + rtol · scale
atol = 1e-5 mm,  rtol = 1e-4
scale = |EFL|               for the effective focal length
scale = max(|BFL|, |EFL|)   for the back focal length
```

Each quantity carries its own `scale_mm` and `scale_basis` in `comparison`, so the rule stated here
is the rule that was applied.

`comparison` records the rule, both values, the absolute difference, the tolerance and
`fraction_of_tolerance` for each quantity, so a reviewer recomputes the verdict instead of trusting
it. The tightest record in the seed batch uses 0.43 % of its allowance (`1792917`, EFL), so the rule
is not tuned to pass.

`validation.paraxial` is `MATCHED` only when both quantities are within tolerance.

## Aperture

An f-number, an entrance pupil diameter, an object-side numerical aperture and a paraxial working
f-number are different quantities with different conditions. Each is its own field and none is
derived from another. Exactly one field per record is marked `system_aperture: true`: the aperture
the design file declares.

### The `FNUM` type mapping

A ZMX `FNUM` line carries a type flag in its second field, and the flag — not a numerical
coincidence with another convention — decides which f-number the value is:

| `FNUM` flag | Convention | Field stored | Records |
|---|---|---|---|
| `0` | Image Space F/# | `f_number` | 19 |
| `1` | Paraxial Working F/# | `paraxial_working_f_number` | `6744570a/b/c` |

The mapping is verified against the upstream dump's declared `System Aperture`, which agrees with
the flag on every record.

### System-aperture fields

| Field | Meaning |
|---|---|
| `f_number` | Image-space f-number, from `FNUM` with flag 0. |
| `paraxial_working_f_number` | Paraxial working f-number, from `FNUM` with flag 1. |
| `entrance_pupil_diameter_mm` | From the design file's `ENPD`. |
| `object_numerical_aperture` | From the design file's `OBNA`. An NA is not an f-number. |

`why_not_design_file` is the marker for a system-aperture value that comes from the dump because the
design file states no aperture at all (`FNUM`, `ENPD` and `OBNA` all absent). **No record in the seed
batch takes that route**: every one of the 39 states its aperture in the design file, so the field is
documented here for the contract's sake and does not occur in the data. The property list is kept as
`property_list_comparison` only. A record with no stated aperture leaves every system-aperture field
out rather than inferring one.

### Dump-derived f-number facts

The dump prints several f-number conventions side by side. Each is kept as its own fact with
`system_aperture: false` and a `source_locator` naming where it came from, and is **never** merged
into the system-aperture value:

| Field | Meaning |
|---|---|
| `image_space_f_number` | The dump's stated image-space F/#. |
| `paraxial_working_f_number_from_dump` | The dump's stated paraxial working F/#. |
| `working_f_number` | The dump's stated working F/#, computed by tracing rays. |

### Stop validity: `design.aperture_validity`

A record's eligibility for stop-dependent evaluation is machine-readable, not prose:

| Field | Meaning |
|---|---|
| `state` | `STOP_BLOCKING`, `STOP_NOT_BLOCKING` or `NO_STOP`. |
| `stop_blocks_light` | Whether the Project's stop plate actually blocks light outside its opening. |
| `basis` | Why the state holds. |
| `consequence` | What the state means for the record, in prose. |
| `eligible_for_stop_dependent_evaluation` | Whether the record may be evaluated at all against a stop-dependent constraint. |
| `satisfies_stop_dependent_constraints` | Whether the record may claim a stop-dependent **match**. |
| `satisfaction_basis` | Why that claim is or is not available. |

The two levels are deliberately separate. A blocking plate makes a record **eligible for
evaluation**; it does not by itself establish a stop-dependent match, which needs the relevant
evidence — a field point, a pupil sample, an illumination measurement. This batch has none of those,
so `satisfies_stop_dependent_constraints` is `false` on every record: 38 are eligible
(`STOP_BLOCKING`), one is not (`7821720c`, `STOP_NOT_BLOCKING`, whose plate equals its opening and
whose Project is a first-order geometry reference). Claiming a match from geometry alone would repeat
the mistake of claiming coverage from sampling.

## Field

The **design file is authoritative**: the ZMX states the field type in `FTYP` and the field points
in `YFLN`. `FTYP`'s first code maps to the kind — `0` angle, `1` object height, `2` paraxial image
height, `3` real image height — and the record keeps that kind:

| `kind` | Fields | Meaning |
|---|---|---|
| `ANGULAR` | `full_field_deg`, `half_field_deg`, `field_points_deg` | Field angles. The full field is twice the largest stated half field. |
| `OBJECT_HEIGHT` | `max_object_height_mm`, `field_points_mm` | Object-space field size in millimetres, not an angle. |
| `IMAGE_HEIGHT` | `max_image_height_mm`, `image_height_kind`, `field_points_mm` | Image-space field size. An image quantity: neither an object-space field nor an angle. |

The three are not interchangeable and the import never converts one into another.

Two checks sit beside the value and are **not** values:

- `prescription_dump_check` — the upstream dump prints the same field table. `MATCHES` when it
  agrees, otherwise `FIELD_TABLE_DIFFERS` with both numbers. Across this batch it is `MATCHES` for
  all 39 accepted records.
- `property_list_comparison` — the collection's `lens_properties_list.txt` is a hand-maintained
  summary whose field convention is **not consistent across rows**: an angle-typed row may give the
  full field (`528155`: 53.2) or a half field (`5852515a`: 4.0), and two angle-typed rows carry an
  object height instead (`6016226`: 1.673, `Kidger2004`: 0.315). It is therefore never used as a
  field value and never described as a source disagreement.

A missing value is left out. An unknown field leaves the field constraint unconfirmed.

## Provenance checks

These members compare source data. They are kept for review, are never values, and the retriever
neither matches nor ranks them (they are not typed facts; a constraint reaching one is
UNCONFIRMED `COMPARISON_ONLY`, and naming one as a target, preference, range or field is an
error):

| member | compares |
|---|---|
| `first_order.engine_check` | engine paraxial values against the source's |
| `aperture.property_list_comparison`, `field.property_list_comparison` | the collection's property list |
| `field.prescription_dump_check` | the upstream dump's field table against the design file's |
| `materials.catalog_comparisons` | published catalog glasses of the same name |

A new check member must be added to `fact_extraction.check_members` in
`src/os_optics_lens/vocabulary.json` and to the evaluator.

## Sampling grids

`sampled_field_fractions` on a performance entry (or another fact object) lists the ordered
sample positions behind a sampled value such as an interval MTF minimum. It is a comparison
condition of that value: queries pin it with `required_sampled_field_fractions`, and `compare`
treats different grids as not comparable.

## Evidence extracts

An evidence extract is a committed JSON file that holds the sourced data a record's facts rest on.
Every extract follows one contract:

- the top level is a JSON object;
- `schema` is `"opensci.lens.evidence_extract"`;
- `record_id` (a string) names the record the extract belongs to;
- `sections` is a non-empty object, and no member of it is `null`. A section is the unit a
  reference addresses; its value is any other JSON value;
- every other top-level member is metadata (for example `source`, `license`, `scope`,
  `upstream_archive`, `schema_version`). It travels with the file but is **not addressable**: no
  reference and no `inspect --evidence` name reaches it.

References:

- a fact's `source` is `{path, sha256, section}` and names one member of `sections`;
- `collection.evidence_extract` is `{path, sha256}` and names the record's extract as a whole, so
  `inspect --evidence` can address each of its sections;
- `path` is corpus-relative with `/` separators, and `sha256` is the SHA-256 of the file's
  LF-normalized bytes.

Strict intake enforces the object root, the `schema` string and the `sections` object
(`EVIDENCE_MALFORMED`, naming the file and the field; a warning under
`--allow-unresolved-evidence`, and the reference then stays unresolved). The index stores only
conforming extracts and re-hashes their bytes on every read.

## Member types

The builder indexes, hashes or iterates the members below, so each must have its stated type when
present (`null` counts as absent unless noted). A violation is an error finding naming the file and
the field (`INVALID_FIELD` unless noted), never a crash.

| member | type |
|---|---|
| `aperture`, `cost_proxy`, `field`, `first_order`, `image_format`, `spectrum`, `validation`, `analysis_evidence` | object |
| each `analysis_evidence.<study>`, its `status` | object, string |
| every section, fact object and performance entry: `basis`, `definition`, `condition`, `eligible_for`, `state` | string |
| the same: `conditions` | object or string |
| the same: `system_aperture` | boolean |
| the same: `sampled_field_fractions` | non-empty list of finite numbers |
| the same: `source` | evidence reference object (`EVIDENCE_MALFORMED`, downgradable) |
| a section's `<member>_basis` | string |
| `design.lens_type`, `design.design_scope`, `design.conjugate` | string |
| `design.module_role` | non-empty string; `null` is invalid (see below) |
| `design.aperture_validity` | object |
| `collection.license`, its `copyright` and `spdx` | object, string |
| `collection.evidence_extract` | object; `null` is invalid (`EVIDENCE_MALFORMED`, downgradable) |
| `native_project.engine_version`, `validation.paraxial`, `validation.field`, `validation.project_check` | string |
| a Project's `configurations[].label` and `.name` | string (`PROJECT_CONFIGURATION_INVALID`) |
| a batch manifest's `collection`, `collection.collection_id` | object, non-empty string (both required) |

A record, Project, batch manifest or evidence file whose top level is not an object (the literal
`null` included) is an error finding too; a discovered file is never skipped silently.

### `design.module_role`

Optional. For a phone module, where it sits in a device: `main_camera`, `ultrawide`, `telephoto`
or `front_camera` (aliases `main`, `ultra_wide`, `tele`, `front`). It is a record-level facet, not
a statement about what the prescription is. Another string is indexed verbatim with an
`UNKNOWN_TERM` warning.

## Materials

`materials.model_glasses` holds every glass defined from the source's own per-surface index table
in the upstream prescription dump: a 3-term Sellmeier with fixed poles solved to those samples
(`basis` records the poles, the samples and the residual), or a constant index when the source
states a single wavelength. A model glass always names its dispersion model and parameters;
`nd`/`Vd` alone do not define a curve.

`materials.catalog_comparisons` records where a published catalog glass of the same name exists,
with the largest index difference. The catalog glass is **compared, never substituted**: the
record keeps the source's own indices.

A continuous law is only fitted to samples that can come from one glass. A solid glass in the
visible/near-infrared has normal dispersion — its index falls as the wavelength rises — so samples
that rise or are not monotonic cannot support a fit: interpolating between them would invent a
material. Such an input is deferred (`MATERIAL_MODEL_FAILED`) instead of being modelled.
`lenslibrary-895045b` is that case: its surface 1 states 1.7517, 1.6042, 1.7338 at 486, 588 and
656 nm.

## Approximations and unsupported constructs

`approximations` lists every modelling decision that changes the optics or its interpretation,
with a stable `code`. `unsupported_constructs` lists what the source contains and this record does
not represent. An accepted record has no construct in that list that affects the design.

## Validation states

`validation` tracks four independent outcomes:

| Key | Values |
|---|---|
| `conversion` | `PASSED` / `REFUSED` |
| `project_check` | `ACCEPTED` / `REFUSED` |
| `optical_solids` | `VALID` / `INVALID` |
| `paraxial` | `MATCHED` / `DIFFERS` / `NOT_CHECKED` |

A record is accepted only when all four hold. `MATCHED` means the engine's paraxial EFL and BFL
reproduce the source-stated values within the tolerance stated in the import notes.

## Batch manifest

`batch-lenslibrary-seed-1.json` reports an outcome for **every** input:

| `outcome` | Meaning |
|---|---|
| `ACCEPTED` | A committed record exists; `record_id` names it. |
| `DEFERRED` | Deliberately not converted, with `reason_code` naming the construct or the missing source data. |
| `CONVERSION_FAILED` | The engine refused the surface table, with the engine's own problems attached. |

`counts` and `accepted_record_ids` are generated from the rows, so the manifest cannot drift from
the records.

## Record schema version 1.1: configurations

`record_schema_version` `"1.1"` adds one optional top-level member, `configurations`. It is
additive: every other field keeps the meaning stated above.

A record uses it when its native Project declares several `os-optics` configurations (for
example the wide and tele positions of a zoom). Each one is a separate candidate that a query can
match:

```json
"configurations": [
  {"configuration_id": "Wide", "label": "wide end",
   "first_order": {...}, "aperture": {...}, "field": {...}, "performance": [...]},
  {"configuration_id": "Tele", "label": "tele end",
   "first_order": {...}, "aperture": {...}, "field": {...}, "performance": [...]}
]
```

### Allowed keys

An entry may contain exactly these keys:

- `configuration_id` (required);
- `label` (optional);
- the section overrides `conjugate`, `first_order`, `aperture`, `field`, `image_format`,
  `spectrum`, `performance`, `analysis_evidence` and `cost_proxy`.

Any other key is `INVALID_FIELD`.

### Identifiers

- `configuration_id` must match exactly an id that the native Project declares in
  `configurations[].configuration_id`.
- A Project id must start with a letter, may contain only letters, digits, `_` and `-`, and may be
  at most 64 characters long. Ids must be unique case-insensitively and must not be a reserved
  device name.
- Two entries may not share an id (compared case-insensitively).

### Configuration view

Each entry defines a configuration view:

- the record is copied without `configurations`;
- every section the entry contains **replaces** the top-level section of the same name, and is
  never merged into it; an entry's `conjugate` replaces `design.conjugate`;
- the view also carries `configuration = {configuration_id, label, native: true}`.

Every constraint of a query is evaluated against one view. A record never matches because
different configurations satisfy different constraints.

### Sections that cannot be inherited

`first_order`, `aperture`, `field`, `performance` and `design.conjugate` depend on the
configuration. With `configurations`, each of them must either be absent at the top level or be
overridden by every entry. Otherwise one configuration would silently inherit another's values,
which is `INVALID_FIELD`. Sections that do not vary, such as `spectrum` or `image_format`, may stay
at the top level and are shared.

### Count and hash

- `design.configuration_count`, when present, must equal the number of entries.
- `record_hash` covers `configurations` like any other member.

### Compatibility

Every existing `1.0` record is valid unchanged. A record without `configurations` (every `1.0`
record, and a `1.1` record that omits the member) is indexed as exactly one configuration:

- If its native Project declares no configurations, the configuration is the Project as written:
  `configuration_id` is `null`, `native` is false and the label is `"project as written"`.
- If the Project declares exactly one configuration, the single indexed configuration takes that
  native id and label.
- If the Project declares two or more configurations, the record must map them in
  `configurations` (`CONFIGURATION_UNMAPPED`).

A `1.0` record must not carry `configurations`.

### How the index reads records

[INDEX_SCHEMA.md](INDEX_SCHEMA.md) gives the index tables and every build finding.
[RETRIEVAL.md](RETRIEVAL.md) describes how the index reads records: discovery, configuration views,
facets, typed facts, evidence resolution and the query semantics.

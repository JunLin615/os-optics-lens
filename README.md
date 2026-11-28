# os-optics-lens

<!-- BEGIN INDEX COUNTS -->
**2,453 searchable records · 2,464 configurations**
<!-- END INDEX COUNTS -->

A searchable library of optical prescriptions for AI agents. Start with an optical design brief, inspect relevant facts and structural details, and retrieve an editable native Project to continue design in `os-optics`.

## Project rules

- Search the available native Projects using their own optical values. Return short candidate summaries with the facts that help an agent choose and open a Project.
- Keep the corpus focused on Projects, compact search metadata and any small supporting data needed to interpret a Project fact. Keep preparation material in the contribution workspace or PR. Record the contributor's direct source and attribution, available through explicit provenance inspection.
- Keep a specification match and a promising optimization starting point distinguishable in search results. Show a missing value when it matters to the query.

`os-optics` owns the native model and optical evaluation. `os-optics-lens` ships the prescription collection and its retrieval interface. `os-optics-kit` provides agent skills, study tools and worked cases. [Engine](https://github.com/JunLin615/os-optics) · [Kit](https://github.com/JunLin615/os-optics-kit)

To add a prescription, follow [Submit a native Project](CONTRIBUTING.md#submit-a-native-project). The seed below preserves its original intake metadata.

## The seed collection

`records/lenslibrary/` holds the first records: native `os-optics` Projects converted from
Nathan Hagen's LensLibrary at a pinned revision. Each record directory holds the Project and a
`record.json` with the search facts, their basis and the provenance. The batch outcome manifest
reports an outcome for every source input.

| | |
|---|---|
| Source inputs | 77 ZMX files at revision `86b2c0ca` |
| Accepted records | 39 |
| Deferred, with a reason | 32 |
| Conversion failed | 6 |

Read the Project directly, or start from a record:

```
os-optics project check records/lenslibrary/lenslibrary-528155/project.json
os-optics project solids records/lenslibrary/lenslibrary-528155/project.json
os-optics project paraxial records/lenslibrary/lenslibrary-528155/project.json \
    --components LENS --wavelength-nm 587.7220645720764
```

[Record layout and field semantics](docs/RECORD_FORMAT.md) defines the metadata.
[Importing the LensLibrary seed collection](docs/LENSLIBRARY_IMPORT.md) records the conversion
method, the material and surface decisions, the verification run and the known limits.

## The Nikon FX expansion series

`records/nikon-fx/` holds a series of Nikon-branded FX photography lenses, prepared for
[issue #11](https://github.com/JunLin615/os-optics-lens/issues/11). Two sources are used, and a
record says which one it came from.

| Batch | Source | Claim |
|---|---|---|
| `batch-nikon-fx-1.json` | A numerical example printed in a US patent publication | The surface table is transcribed from the publication |
| `batch-nikon-fx-2.json` | The [FlareSim lens corpus](https://github.com/LocalStarlight/flaresim_nuke) at revision `0e28ead9` (MIT) | Each Project models a corpus prescription using catalog glass approximations |

| | |
|---|---|
| Corpus files at the pinned revision | 1370 |
| Nikon-branded files | 246 |
| Distinct source inputs in batch 2 | 202 |
| Accepted records in batch 2 | 101 |
| Deferred, with a reason code | 100 |
| Conversion failed | 1 |

The series contains 102 native Projects including the patent example. Each zoom Project represents
one position. [Nikon FX intake](docs/NIKON_FX_INTAKE.md) records the sources, conversion decisions,
acceptance checks and limits.

## Canon full-frame photography lenses

The Canon collection includes 18 native Projects from published patent numerical examples for
full-frame photography, including wide-angle, telephoto and individual zoom positions. Each
record identifies the publication, example and modeled configuration. The
[collection notes](docs/CANON_FULL_FRAME_INTAKE.md) describe the material and aperture models
and the conditions for the retained chief-ray traces.

### Spectral band boundaries

`band_facets` in a record uses these boundaries, in vacuum nanometres:

| Facet | Interval (nm) |
|---|---|
| `extreme_ultraviolet` | 0.1 – 10 |
| `ultraviolet` | 10 – 380 |
| `visible` | 380 – 780 |
| `near_infrared` | 780 – 3000 |
| `mid_wave_infrared` | 3000 – 8000 |
| `long_wave_infrared` | 8000 – 15000 |

A facet is a **discovery** label derived from the record's sampling span. A hard band constraint
uses confirmed continuous coverage of the Project. The state is `UNCONFIRMED` when the record
supplies only discrete samples or a sampling span.

## The telescope and observation collection

`records/telescope-observation/` holds the second collection: nine classical eyepieces from
MIL-HDBK-141 section 14, one Nikon eyepiece numerical example, and four MIT-licensed sample designs
— refracting telescope objectives and eyepieces. Every record is a coaxial sequential refracting
prescription with a real image plane, because that is what the prescription importer accepts;
reflecting systems, prism erectors and afocal trains are deferred with a reason rather than
approximated.

| | |
|---|---|
| Candidates examined | 88 |
| Accepted records | 14 |
| Deferred, with a reason | 74 |
| Conversion failed | 0 |

[Collection README](records/telescope-observation/README.md) covers the scope, the record table, the
aperture and stop conventions, the validation run and the deferred sources.
[Batch manifest](records/telescope-observation/batch-telescope-observation-1.json) reports one
outcome per candidate.

## Search the collection

The `os-optics-lens` package ships the retriever together with a prebuilt index and the
collection it indexes. Queries run locally, using only the Python standard library (Python 3.10
or newer). Every command prints one bounded JSON document.

Until the first PyPI release, build the wheel from a checkout and install it:

```
uv build --wheel
pip install dist/os_optics_lens-0.1.0-py3-none-any.whl
os-optics-lens help quickstart
```

From brief to an editable Project:

```
os-optics-lens facets --facet design_type --facet image_format
os-optics-lens fields --fact image_format.image_circle_diameter_mm
os-optics-lens search --mode specification --filter design_type=camera_lens \
  --where 'first_order.effective_focal_length_mm between_inclusive [17.1,18.9]' \
  --where 'image_format.image_circle_diameter_mm gte 43.27'
os-optics-lens project yutong-2025-mirrorless-18-p2b --out work/p2b.project.json
```

- **Specification search** reports each hard constraint as `MATCH`, `DOES_NOT_MATCH` or
  `UNCONFIRMED`, with a reason code for each unknown.
- **Starting-point search** (`--mode starting_point --target FACT=VALUE~TOL`) ranks near matches
  and lists their shortfalls and unknowns.
- `compare` compares selected Project facts. `inspect` shows Project facts, configurations and
  prescription details. Use `inspect ID --evidence SECTION` to read supporting analysis.
- `project --out` writes the exact native Project bytes. Their SHA-256 equals the record's
  `native_project.sha256`.
- Use `inspect ID --provenance` when you need the record's direct source, attribution or
  redistribution terms.

On Windows PowerShell, pass JSON requests with `--request FILE`. Outside an installed wheel,
build an index from a checkout with `os-optics-lens index build --corpus . --out
build/os-optics-lens-index.sqlite`, then pass `--index` to each command.
[Retrieval](docs/RETRIEVAL.md) documents installation, the request and response formats, the
search semantics, the error codes and how to rebuild the index.
[Index schema](docs/INDEX_SCHEMA.md) documents the index tables and build findings.

## Status and contributions

The first package ships the search interface and the searchable collection together. The seed
collection is the first batch of that collection, and new series are added as further batches.
[LensLibrary](https://github.com/nzhagen/LensLibrary)

## The phone-module collection

`records/phone-modules/` holds the first expansion batch: seven-element main-camera and
five-element ultrawide modules transcribed from two granted US patent publications. Each record
names its table and embodiment, carries the search facts with their basis, and holds a compact
evidence extract beside the Project.

| | |
|---|---|
| Source examples | 22, across US9366845B2 and US10247911B2 |
| Accepted records | 11 (6 main camera, 5 ultrawide) |
| Deferred, with a reason | 11 |

[Phone main-camera and ultrawide modules](docs/PHONE_MODULE_INTAKE.md) records the transcription
rules, the material and aperture models, the field evidence, the verification run and the limits.

Use [ARCHITECTURE.md](ARCHITECTURE.md) for the design, [AGENTS.md](AGENTS.md) for repository rules,
and [CONTRIBUTING.md](CONTRIBUTING.md) for proposals, single-record contributions and batch intake.
The project uses the [MIT License](LICENSE).

## The phone-telephoto collection

`records/phone-telephoto/` holds the telephoto batch: five-element straight telephoto modules
transcribed from two granted US patent publications, in a short group (5.7–7.0 mm focal length) and
a long group (8.6–10.1 mm). Each record names its table and embodiment, marks itself
`module_role: telephoto` and `folded: false`, and holds a compact evidence extract beside the
Project.

| | |
|---|---|
| Source examples | 22, across US20220299735A1 and US11372203B2 |
| Accepted records | 7 straight telephoto modules |
| Deferred, with a reason | 15 (13 near-duplicate variants, 2 with two aperture stops) |

[Phone telephoto modules](docs/PHONE_TELEPHOTO_INTAKE.md) records the transcription rules, the stop
and tolerance decisions, the field evidence, the verification run and the limits. Folded periscope
designs are out of scope for the surface-table format.

## Data sources and acknowledgments

The initial prescription records are derived from Nathan Hagen's [LensLibrary](https://github.com/nzhagen/LensLibrary), using [revision `86b2c0ca`](https://github.com/nzhagen/LensLibrary/tree/86b2c0cac5a67e2112d58518230c4d8728154f4e). Thanks to Nathan Hagen for compiling and sharing the collection. See the [LensLibrary MIT notice](licenses/LensLibrary-MIT.txt) for the upstream copyright and license. The records identify the library inputs used for conversion.

The telescope and observation collection draws on MIL-HDBK-141 (a US Government work in the public
domain), on US10527839B2 and on the MIT-licensed [optiland](https://github.com/optiland/optiland)
sample designs. Each record states its own attribution and redistribution basis; the [collection
README](records/telescope-observation/README.md) lists the sources and the exact revision read.

The LensLibrary notice must stay with the converted records in any distribution. The repository's
own [MIT License](LICENSE) covers its code and documentation.
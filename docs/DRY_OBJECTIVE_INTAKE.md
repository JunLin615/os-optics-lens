# Dry microscope objectives, batch 1

The accepted Projects model a finite specimen and collimated output. Two nonabsorbing
planes beyond the objective monitor the output path. The manifest defers the
80 mm prescription as a duplicate of the existing `lenslibrary-7643216c` record.

This note records how the first dry-objective batch was prepared so a reviewer can repeat the
conversion and check every claim. It covers the sources, the transcription rules, the aperture and
object-plane decisions, the verification run, and what the records leave unconfirmed. The batch
belongs to the series issue [#26](https://github.com/JunLin615/os-optics-lens/issues/26), which
coordinates objectives that work with air between the specimen or cover glass and the objective.
Immersion objectives are series [#27](https://github.com/JunLin615/os-optics-lens/issues/27).

- **Collection:** `dry-microscope-objectives`
- **Batch:** [`batch-dry-microscope-objectives-1.json`](../records/dry-microscope-objectives/batch-dry-microscope-objectives-1.json)
- **Engine:** public `os-optics` **0.14.0**, no catalog release (every material is defined in the
  Project)
- **Format:** `csv-surface-table/1`
- **Records:** 10 accepted, 9 deferred, 0 conversion failures

## What the batch contains

| Record | Source | EFL (mm) | NA | Track (mm) | Elements / groups | Cover glass | f-number |
|---|---|---|---|---|---|---|---|
| `patent-us11067782b2-obj1` | US11067782B2 obj. 1 | 18.016 | 0.40 | 46.80 | 9 / 6 | 0.17 mm | 1:1.25 |
| `patent-us11067782b2-obj2` | US11067782B2 obj. 2 | 18.015 | 0.40 | 48.00 | 9 / 6 | 0.17 mm | 1:1.25 |
| `patent-us11067782b2-obj3` | US11067782B2 obj. 3 | 18.012 | 0.40 | 49.13 | 9 / 6 | 0.17 mm | 1:1.25 |
| `patent-us11067782b2-obj4` | US11067782B2 obj. 4 | 18.015 | 0.40 | 46.71 | 9 / 6 | 0.17 mm | 1:1.25 |
| `patent-us11067782b2-obj5` | US11067782B2 obj. 5 | 18.017 | 0.40 | 46.69 | 9 / 6 | 0.17 mm | 1:1.25 |
| `patent-us11067782b2-obj6` | US11067782B2 obj. 6 | 18.024 | 0.40 | 49.35 | 9 / 6 | 0.17 mm | 1:1.25 |
| `patent-us11067782b2-obj7` | US11067782B2 obj. 7 | 18.019 | 0.40 | 50.64 | 9 / 6 | 0.17 mm | 1:1.25 |
| `patent-us11067782b2-obj8` | US11067782B2 obj. 8 | 18.019 | 0.40 | 47.65 | 9 / 6 | 0.17 mm | 1:1.25 |
| `patent-us7663807b2-ex1` | US7663807B2 embodiment 1 | 17.987 | 0.80 | 82.75 | 14 / 9 | 0.17 mm | 1:0.8 |
| `patent-us7663807b2-ex2` | US7663807B2 embodiment 2 | 17.940 | 0.80 | 83.54 | 14 / 9 | 0.1757 mm | 1:0.8 |
| `patent-us7643216b2-ex3` | US7643216B2 example 3 | 79.972 | 0.344 | 102.61 | 8 / 6 | none | 1:1.45 |

All lengths are millimetres. EFL and track come from the committed Project; NA, focal length and
cover-glass thickness are the publication's printed values.

Two families are represented: a **10× / 0.40** group of eight variants that differ only in their
aberration balance and glass choice (one family, eight records — `design_family_id`
`us11067782b2`), a **10× / 0.80** pair with a thick meniscus front group, and one **stereomicroscope
objective** of 80 mm focal length and 1:1.45 aperture ratio. The eight 0.40 variants are kept
separately because each is a distinct prescription with its own radii, glasses and air gaps; the
family id groups them without merging them.

## Sources

| Publication | Assignee | Examples in the document | Used here |
|---|---|---|---|
| [US11067782B2](https://patents.google.com/patent/US11067782B2/en), *Microscope objective* | Olympus Corporation | 8 | 8 |
| [US7663807B2](https://patents.google.com/patent/US7663807B2/en), *Microscope objective* | Olympus Corporation | 2 objectives + 1 tube lens | 2 |
| [US7643216B2](https://patents.google.com/patent/US7643216B2/en), *Microscope objective* | Carl Zeiss MicroImaging GmbH | 4 | 1 |

The numerical examples are a public record of a granted patent; the patent document itself is not
redistributed. The transcription, the native Project and the evidence extract are MIT. A numerical
example is a **published design**, not a measured product, and nothing in the batch claims that a
shipped objective uses it.

## Conversion method

1. **Read the publication** as text and isolate the numerical table of the example. The three
   sources print three different layouts: a plain grid (`s | r | d | nd | νd | glass`), a row-pair
   layout (`r 1 = … | d 1 = … | n d1 = …`), and a vertical layout where a radius sits on one row and
   its distance on the next with the lens number in the left column.
2. **Transcribe** radius, distance, index and Abbe number verbatim. `∞` becomes a plane; `−`
   (U+2212) becomes an ASCII minus; nothing is re-signed, re-rounded or re-spaced. Index and Abbe
   number go on the row where the glass **starts**.
3. **Import** with the engine's own importer, which performs the element and stop decomposition and
   the edge and solid checks.
4. **Assemble the Project**: one `COMPOUND` unit, a source at the stated object plane and reference
   wavelength, and one observation plane.
5. **Check** with `project check` and `project solids`, and **verify** with the engine's thick-lens
   paraxial matrix against the printed focal length.

### The reference wavelength

Two conventions occur and each record states its own:

- `US11067782B2` and `US7643216B2` print `nd`/`νd` and `n_e`/`ν_e` **as symbols**, and define the
  focal length at the **e-line**. The records evaluate at 546.07 nm air → **546.219161 nm vacuum**.
- `US7663807B2` defines `n_d`/`ν_d` at the **d-line**: 587.56 nm air → **587.720064 nm vacuum**.

The air-to-vacuum conversion uses the engine's own air model (`material_physics.air_index_exact`),
once, and both values are stored (`sampling_wavelengths_nm`, `sampling_wavelengths_air_nm`).

A consequence is recorded rather than hidden: the Olympus tables print d-line indices while the
design is specified at the e-line, so the dispersion curve is anchored at the printed `nd` and
evaluated at the e-line. The residual is inside the paraxial tolerance on every record (at most
25 % of the allowance) and is listed as `ND_VD_DISPERSION_MODEL`.

### The object plane

A microscope objective images a specimen at a finite distance, so every record needs an object
plane:

- `US11067782B2` tabulates the cover glass as its first two surfaces and states `H`, the
  object-to-last-surface distance (46.69 … 50.64 mm). `H` reproduces the sum of the printed
  distances to 0.003 mm, which shows the object sits **on the front face of the cover glass**; the
  record therefore uses `object_distance_mm = 0.0` and the glass is counted once.
- `US7663807B2` states `d0` (the object-to-first-surface distance) separately for each cover-glass
  thickness: 1.248 mm and 1.3634 mm. The record uses the value from the matching row.
- `US7643216B2` prints the object distance as the first entry of its table (61.00000 mm), and it
  equals the design's front focal distance — the objective collimates, as a stereomicroscope
  objective does.

### Aperture, and what is derived

- `US11067782B2` and `US7663807B2` print **no clear apertures and no stop**. Each semi-diameter is
  derived: the real meridional marginal ray is traced at the stated object-side NA (exact spherical
  intersection and Snell refraction, not a paraxial extrapolation), given a 10 % mechanical
  allowance, floored at the stated object height so the field fits, and reduced where an element
  edge or an air gap would otherwise be impossible. The record carries
  `CLEAR_APERTURES_DERIVED` and `aperture_validity: NO_STOP`. These apertures are **estimates**.
- `US7643216B2` prints neither an NA nor clear apertures. It prints an entrance aperture (53.5 mm)
  and an aperture ratio (1:1.23); the record's NA of 0.344 is derived from those two printed values
  (`NA = D / 2f`), carries `basis: DERIVED`, and its definition says so.

No record claims a stop-dependent match: none carries a field point, pupil sample or illumination
measurement.

### The paraxial rule for this collection

```
|engine − source| ≤ atol + rtol · |source|
atol = 0.005 mm,  rtol = 5 × 10⁻³
```

`atol` covers a focal length printed to two decimals; `rtol` covers radii and thicknesses printed
to four or five decimals together with the engine's air-referenced index convention. The largest
measured deviation in the batch is 0.025 % (`patent-us11067782b2-obj6`, 18.024 mm against 18 mm);
the tightest record uses 6.8 % of its allowance and the loosest 24.9 %.

## Verification

Four outcomes are tracked separately, and a record is accepted only when all four hold.

| Check | Result |
|---|---|
| Conversion (engine importer) | 11 of 11 accepted |
| Native Project check (`project check`) | 11 of 11 `ACCEPTED` |
| Optical solids (`project solids`) | 11 of 11 `VALID` |
| Paraxial EFL against the printed focal length | 11 of 11 `MATCHED`, at most 24.9 % of the allowance |

Reviewer commands, from the repository root:

```console
os-optics project check records/dry-microscope-objectives/patent-us11067782b2-obj1/project.json
os-optics project solids records/dry-microscope-objectives/patent-us11067782b2-obj1/project.json
os-optics project paraxial records/dry-microscope-objectives/patent-us11067782b2-obj1/project.json \
    --components LENS --wavelength-nm 546.219161
python -m unittest discover -s tests -v
```

`tests/test_dry_objectives.py` re-derives every record hash, the prescription hash from the
committed evidence text, the Project and evidence hashes, the comparison verdict, the family
grouping and the manifest reconciliation, and runs without the engine.

## What the records do not claim

- **No MTF, relative illumination or distortion values.** The publications print curves, not tables
  of numbers under stated conditions.
- **No continuous spectral coverage.** One reference wavelength per record; the span is
  discovery-only.
- **No stop-dependent match.** Two families carry no stop at all; the derived apertures are
  estimates.
- **No measured working distance.** The value is the stated object distance, or that distance minus
  the cover glass; a publication that prints neither leaves it unconfirmed.
- **No magnification**, except where the publication prints one. `US11067782B2` states only that
  the class runs "around 10×", which is not a per-example value and is not recorded as one.
- **No mechanical data.** Clear apertures are derived, and the engine reports the edge-thickness
  review findings that follow from them as `INFO` entries in `validation.solids_findings`.

## Deferred and unavailable sources

| Source | Outcome | Reason |
|---|---|---|
| `US7643216B2` examples 1, 2, 4 | `DEFERRED` (`PARAXIAL_DIFFERS`) | The transcribed table misses the printed focal length by 1.2 %, 0.5 % and 2.5 % (2.4×, 1.05× and 4.97× the tolerance). Example 4's printed quantities are also mutually inconsistent (f′ = 65.59 mm, 1:1.23, 53.5 mm entrance aperture). Kept out until resolved rather than merged with a note. |
| `US7663807B2` numerical data 3 | `DEFERRED` (`OUT_OF_SCOPE_TUBE_LENS`) | The 180 mm tube lens used with embodiments 1 and 2 is not a microscope objective. |
| `US9746658B2`, `US8988780B2`, `US7995272B2` | `DEFERRED` (`IMMERSION`) | Liquid-immersion, oil-immersion and immersed-specimen designs; series #27 covers them. |
| `US6069744A` | `DEFERRED` (`NO_NUMERICAL_TABLES`) | The rendered description carries no numerical prescription, only citation tables. |
| Further US, EP and WO objective publications | Not assessed | The full-text source rate-limited this session before they could be read. The series issue keeps the wider inventory; this batch is one contribution to it. |

## Reproducing the batch

The preparation workspace holds the page reader, the table extractor, the specification files, the
builder and the record writer. They are source-specific tooling, kept out of the corpus on purpose:

```
spec (<record>.presc)  →  surface_table.csv  →  project.json  →  record.json + evidence.json
```

Each step is deterministic: the same specification produces the same surface table, the same Project
bytes and the same record hash. `collection.source_input_sha256` pins the transcription and
`record_hash` pins the record, so a reviewer can detect an edited record without the engine.

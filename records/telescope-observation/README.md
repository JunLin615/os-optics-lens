# Telescope and observation optics

The Huygenian Project records its internal focus without absorbing the beam
and monitors the beam downstream of the last element. The 48-inch objective
uses millimetres for all native dimensions, converted with 25.4 mm per inch.

Prescriptions for telescopic and observation imaging: classical eyepieces, refracting telescope
objectives, and a small set of open sample designs. Part of the [expansion
series](https://github.com/JunLin615/os-optics-lens/issues/36) "Telescope and observation optics".
Telescope architecture is a retrieval facet; dedicated camera products, microscope objectives and
spectrometer paths belong to their own series.

Every record here is a **refracting, coaxial, single-configuration** prescription with a real image
plane, because that is what the engine's `csv-surface-table/1` importer accepts. Reflecting and
catadioptric telescopes, prism erectors and afocal trains are deferred, not approximated: see
[the batch manifest](batch-telescope-observation-1.json).

## Records

| Record | Design | Type | Elements | EFL (mm) | Aperture | Field (full) | Paraxial |
|---|---|---|---|---|---|---|---|
| `mil-hdbk-141-t14-1-huygenian` | Huygenian eyepiece | eyepiece | 2 | 25.4 | 5 mm pupil | 30° | `MATCHED` |
| `mil-hdbk-141-t14-2-ramsden` | Ramsden eyepiece | eyepiece | 2 | 25.4 | 5 mm pupil | 30° | `MATCHED` |
| `mil-hdbk-141-t14-3-kellner` | Kellner eyepiece | eyepiece | 3 | 25.4 | 5 mm pupil | 40° | `MATCHED` |
| `mil-hdbk-141-t14-4-orthoscopic` | Orthoscopic eyepiece | eyepiece | 3 | 25.4 | 5 mm pupil | 50° | `MATCHED` |
| `mil-hdbk-141-t14-5-plossl` | Symmetrical (Plössl) eyepiece | eyepiece | 4 | 25.4 | 5 mm pupil | 50° | `MATCHED` |
| `mil-hdbk-141-t14-6-berthele` | Berthele eyepiece | eyepiece | 3 | 25.4 | 5 mm pupil | 50° | `MATCHED` |
| `mil-hdbk-141-t14-7-erfle` | Erfle eyepiece | eyepiece | 5 | 25.4 | 5 mm pupil | 60° | `MATCHED` |
| `mil-hdbk-141-t14-8-modified-erfle` | Modified Erfle eyepiece | eyepiece | 5 | 25.4 | 5 mm pupil | 60° | `MATCHED` |
| `mil-hdbk-141-t14-9-wild` | Wild eyepiece | eyepiece | 5 | 25.4 | 5 mm pupil | 72° | `MATCHED` |
| `optiland-telescope-doublet` | Telescope doublet, f/8 | telescope_objective | 2 | 48.05 | f/8 | 2° | `NOT_CHECKED`¹ |
| `optiland-triplet-telescope-objective` | Air-spaced triplet, f/2.8 | telescope_objective | 3 | 100.12 | f/2.8 | 2° | `NOT_CHECKED`¹ |
| `optiland-48-inch-telescope-objective` | 48-inch objective, f/6 | telescope_objective | 4 | 1220.80 | f/6 | 20° | `NOT_CHECKED`¹ |
| `optiland-erfle-eyepiece` | Erfle eyepiece, 4 mm pupil | eyepiece | 4 | 79.48 | 4 mm pupil | 40° | `NOT_CHECKED`¹ |
| `patent-us10527839b2-ex01` | Nikon eyepiece optical system | eyepiece | 3 | 25.79 | 4 mm pupil | 52.44° | `MATCHED` |

¹ The sample prints no focal length, so no effective-focal-length comparison exists and the
`paraxial` state stays `NOT_CHECKED`. Its own solved back focus **is** compared, and agrees to
0.08–0.15 %. See *What was checked* below.

Each of the nine MIL-HDBK-141 eyepieces is its own design family (`design_family_id`), scaled by the
handbook to a common 25.4 mm focal length and a 5 mm exit pupil, so they compare directly: the set
runs from the two-element Huygenian at 30° full field to the five-element Wild at 72°.

## Sources

| Source | What was used | Redistribution basis |
|---|---|---|
| [MIL-HDBK-141](https://github.com/jfblanchard/MIL-HDBK-141), *Optical Design*, US Department of Defense, 1962 | Section 14, Tables 14.1–14.9 (nine eyepieces), read from the modern transcription in `ocr_output/ch14.md` | US Government work, public domain. The handbook is not redistributed; the transcription, Project and evidence extract are MIT. |
| [US10527839B2](https://patents.google.com/patent/US10527839B2/en), Nikon Corporation | Numerical example 1 | A published patent. Only the transcribed numbers are committed; the document is not redistributed. |
| [optiland](https://github.com/optiland/optiland) at `cd0a756` | `TelescopeDoublet`, `TripletTelescopeObjective`, `TelescopeObjective48Inch`, `EyepieceErfle` | MIT (Copyright (c) 2024 Kramer Harrison). The samples publish these surface data; the design reference each sample names is attributed, not redistributed. |

Each record carries its own attribution, source locator and license block; the batch manifest lists
all three sources with the exact revision read.

## How a record was made

The printed table is transcribed into a `csv-surface-table/1` surface list, imported by `os-optics`
0.14.0, and the engine's own decomposition produces the Project: one `SURFACE_SEQUENCE` per element,
one `APERTURE_STOP` per stop plate, and the image plane at the paraxial focus. Nothing in a Project
is a guess:

- **Radii, thicknesses, indices, Abbe numbers** are copied from the printed table; the evidence
  extract keeps each value with the form it was printed in (`thickness_stated_as`: `0.7062 cm`).
- **Wavelengths** are converted once from the printed air values to vacuum with the engine's own air
  model, so a record's wavelength is the one the engine evaluates. The air values are kept beside
  them.
- **Materials** are constant indices at the reference wavelength, taken from the printed `nd`. The
  printed `vd` is recorded but not modelled: a constant index has no dispersion, so no MTF, spot or
  chromatic claim can rest on it.
- **Clear apertures** are the one derived quantity that changes what the Project transmits, and the
  sources state none. Each element is given one clear diameter: the largest of the paraxial
  marginal-ray and chief-ray envelopes over its own surfaces, times a 2 % mechanical margin. It is a
  minimum clear aperture — every surface of the element passes the axial beam and the chief ray at
  the full stated field — and it is a buildable estimate, not a drawing. No vignetting beyond the
  stop is modelled and no vignetting figure is claimed.
- **The stop** is modelled as the source states it. A plane stop in air becomes an opaque plate with
  the stated opening (the nine MIL eyepieces, `optiland-erfle-eyepiece`). A stop marked on a
  refracting surface (`optiland-telescope-doublet`, `-triplet`) has no plate: its clear aperture
  bounds the beam and the record says so. The 48-inch objective marks a plane stop that coincides
  with the front vertex of the next element, so it is carried as a dummy surface; the record says
  that the stated f-number is realised by the clear apertures instead.
- **The pupil** is recorded under both names where the source states it. An eyepiece's *exit pupil*
  (5 mm for the handbook eyepieces, 4 mm for the optiland Erfle) is what the source declares and is
  the record's `system_aperture`; the same plane, seen from the transcription's direction, is the
  model's entrance pupil and is recorded separately as `DERIVED`. `patent-us10527839b2-ex01` states
  no pupil, so its 4 mm eye pupil is a stated modelling choice, not a source value, and the record
  says so in `aperture.entrance_pupil_diameter_mm.definition`.
- **Orientation**: `patent-us10527839b2-ex01` is printed from the display panel to the eye and images
  to a virtual image, so the table is reversed (radii negated, thicknesses reversed) and a real image
  plane is placed at the paraxial focus. The reversal is stated in the record and in the evidence,
  and the paraxial focal length is unchanged by it.

Each record states its value bases: `SOURCE_STATED`, `DERIVED` or `ENGINE_COMPUTED`. A value the
source does not print is `null`, never inferred, and the corresponding constraint stays
unconfirmed.

## What was checked

Four independent outcomes per record, all recorded in `validation`:

| Outcome | Result across the batch |
|---|---|
| `conversion` | `PASSED` for all 14: the importer accepted the surface table. |
| `project_check` | `ACCEPTED` for all 14: the Project opens and validates. |
| `optical_solids` | `VALID` for all 14: no error and no warning — every element is a closed body with positive glass thickness out to its outline, and consecutive elements do not overlap. The check reports 18 `INFO` edge-thickness facts (elements whose edge thickness is 35–89 % of their clear diameter, above its 35 % review ratio and far below the 150 % warning limit); those are real properties of eyepiece menisci and the records keep them verbatim. |
| `paraxial` | `MATCHED` for 10, `NOT_CHECKED` for 4 (no printed focal length). |

The paraxial comparison recomputes EFL and back focal distance from the committed Project and
compares them with the source under a stated rule (`|engine − source| ≤ 0.005 mm + 3 × 10⁻³ ·
scale`, scale `|EFL|` or `max(|BFL|, |EFL|)`). The tightest record uses 45 % of its allowance, so
the rule is not tuned to pass. Ten records compare both quantities; the four optiland records
compare the back focus against the sample's own solved image distance, because the sample prints no
focal length; `patent-us10527839b2-ex01` compares the focal length only, because its image plane is
derived rather than printed. `MATCHED` therefore means *every quantity the source states* was
reproduced within tolerance — a quantity the source does not state stays `UNCONFIRMED` in
`comparison` and does not block the state, which is why the four optiland records read
`NOT_CHECKED` (no stated focal length) while their back focus is confirmed.

Not checked, and not claimed: MTF, spot size, distortion, chief-ray angle, relative illumination,
chromatic focal shift, tolerance sensitivity, ghost or stray-light analysis, thermal behaviour, and
physical qualification. The material model (constant index) and the derived clear apertures are the
two limits that most affect any of those.

## Batch outcome

`batch-telescope-observation-1.json` reports one outcome per candidate that this batch looked at:

| | |
|---|---|
| Candidates examined | 88 |
| Accepted records | 14 |
| Deferred, with a reason | 74 |
| Conversion failed | 0 |

| Deferred reason | Count | Meaning |
|---|---|---|
| `SOURCE_TABLE_IMAGE` | 30 | the table is an image or the text layer is unusable |
| `OUT_OF_SERIES_SCOPE` | 14 | rigid endoscope relay systems, which the series routes elsewhere |
| `SOURCE_TABLE_DEGRADED` | 10 | text, but the OCR corrupts digits; needs a page-image pass |
| `SOURCE_TRANSCRIPTION_UNCERTAIN` | 9 | read, but a digit or column meaning is unsettled |
| `AFOCAL_SYSTEM` | 4 | no real final image plane |
| `DEFERRED_TO_NEXT_BATCH` | 4 | legible and in scope, held for a later batch |
| `NO_GLASS_DATA` | 1 | radii and thicknesses printed without an index |
| `VARIANT_OUTSIDE_BATCH` | 1 | another example of a family already carried |
| `REFLECTIVE_OR_PRISM` | 1 | the path contains a mirror |

## Limits and what comes next

- **No reflecting systems.** A Newtonian, Cassegrain, Maksutov or Schmidt design cannot be
  transcribed into a sequential refracting table, and an unfolded prism block is not the design.
  These need an engine capability the collection does not have yet.
- **One wavelength per Project.** The three-wavelength samples from optiland are recorded as
  sampling wavelengths, but the committed Project evaluates the primary sample only, so no chromatic
  result follows from these records.
- **Deferred, not lost.** `US2500017A` (three apochromatic objectives with `N`, `V` and partial
  dispersion) and `US2660093A` are legible and in scope. A further pass over modern eyepiece patents
  (Nikon, Nikon Vision, Canon, Fujifilm, Sony: 61 text-extractable examples across 13 publications)
  is prepared and held for the next batch of this series.

## Fields this collection adds to the shared contract

The [shared record format](../../docs/RECORD_FORMAT.md) was written for the LensLibrary seed batch.
These records use its fields unchanged and add the following, all optional and all additive:

| Field | Meaning |
|---|---|
| `spectrum.sampling_wavelengths_nm` with more than one entry | every wavelength the source enables, not just the primary. `spectrum.engine_evaluation_nm` then names the one the committed Project traces, so a multi-sample record never implies a multi-wavelength evaluation. |
| `spectrum.sampling_span_nm.basis = SPAN_OF_STATED_SAMPLING_WAVELENGTHS` | the span of several stated samples; `SINGLE_WAVELENGTH_BAND` still means the ±0.5 nm convenience window. |
| `field.field_points_deg`, `field.field_points_basis` | the field points the source enables, in degrees, half-angle. Absent where the source states only a maximum field. |
| `aperture.exit_pupil_diameter_mm` | the eye-side pupil an eyepiece source states, which is the record's `system_aperture`. Separate from `entrance_pupil_diameter_mm`, which is the modelled, beam-bounding aperture in the transcription's direction. |
| `first_order.engine_check.comparison.back_focal_length.source_mm` | the source's own back focal distance, when it has one: the printed image distance (handbook, patent) or the sample's solved spacing (optiland). Compared under the same rule as the focal length. |
| `validation.solids_findings[].severity = INFO` | the solid check's review-level facts (an element thicker at its edge than 35 % of its clear diameter). Kept, not filtered: they are properties of the design, not defects. `member_id`, `surface_id` and `other_surface_id` name where each one sits. |

A record's `approximations` list is the machine-readable place for the modelling decisions behind
these fields; `validation.notes` carries the prose that a reviewer needs.

## Layout

```
records/telescope-observation/
├── batch-telescope-observation-1.json     one outcome per candidate
├── README.md                              this file
└── <record_id>/
    ├── project.json                       the native os-optics Project (authoritative)
    ├── record.json                        search facts, bases, provenance, validation
    └── evidence.json                      the transcribed table and the stated parameters
```

[Record layout and field semantics](../../docs/RECORD_FORMAT.md) is the shared contract; the
collection-specific decisions above (element clear diameter, stop modelling, the back-focus
comparison) are stated here and repeated in each record's `approximations` and `validation` blocks.
`tests/test_telescope_observation.py` checks the committed files against both.

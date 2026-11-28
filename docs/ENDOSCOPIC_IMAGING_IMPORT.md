# Importing the endoscopic imaging batch

This note records how the first endoscopic-imaging batch was prepared, so a reviewer can repeat it
and check every claim. It covers the source search, the candidate inventory, the conversion method,
the material decision that this source forces, the verification actually run, and what remains open.

- **Series issue:** [os-optics-lens#30](https://github.com/JunLin615/os-optics-lens/issues/30)
- **Engine:** public `os-optics` **0.14.0**, catalog release **none used** (every glass is defined in
  the Project)
- **Format:** `csv-surface-table/1`
- **Batch:** `records/endoscopic-imaging/batch-endoscopic-imaging-1.json`

The current manifest lists the accepted records and the deferred configurations.
The native finite-object entries place the source waist at their object plane.

## Source search

The series asks for an inventory of candidates with a batch plan, so the search is recorded rather
than only its result. Google Patents was queried through its public JSON endpoint
(`patents.google.com/xhr/query`) with eight phrasings:

| Query | Restriction | Hits |
|---|---|---|
| `"endoscope objective lens"` | US | 172 |
| `"objective lens for endoscope"` | US | 81 |
| `"endoscope objective optical system"` | US | 79 |
| `"objective optical system for endoscope"` | US | 79 |
| `"imaging optical system" "endoscope" "objective"` | US | 825 |
| `"rigid endoscope" objective lens` | US | 3472 |
| `"endoscope" "objective" lens "numerical aperture"` | JP | 641 |
| `"objective lens" endoscope "wide angle"` | US | 559 |

The eight result sets were merged by publication number, giving **535 unique publications**. They
were ranked by whether the title names an endoscope objective and whether the publication is a
granted document, and the top 70 were fetched. Of those, **25 publications carry at least one
extractable surface table**, and those tables split into **168 candidate prescriptions** (one per
example).

`records/endoscopic-imaging/` holds the accepted records. The candidate inventory, with the verdict
for every candidate, is in the batch manifest's `outcomes` and in the series issue's batch plan.

## What decides a candidate

A candidate is convertible only when the whole prescription is a coaxial, sequential, refracting
surface table:

| Construct | Outcome |
|---|---|
| A reflecting surface (an oblique-viewing prism, a folded path) | deferred, `REFLECTOR` |
| A tilt or a decentre | deferred, `UNSUPPORTED_TILTSURF` |
| A grating, an ideal paraxial surface, an `a2` term | deferred, the engine's own code |
| A variable spacing with no stated value | deferred, `MULTI_CONFIGURATION` |
| A variable spacing with a stated value for one state | accepted for that state, with the state named |
| A zero spacing between two surfaces | the dummy row is merged away, `DUMMY_SURFACE_MERGED` |
| A stop with a zero spacing to the next surface | carried as that surface's clear aperture, `STOP_MERGED_INTO_REFRACTING_SURFACE` |
| A stated clear aperture (`D/2`) | used verbatim |
| No stated clear aperture | derived, `SEMI_DIAMETER_DERIVED` |

### Two geometry decisions worth naming

**A zero spacing is not a spacing.** When a patent puts a stop and the next surface at the same
vertex, the format has no way to express it: a zero thickness is refused, and a nominal gap would
invent geometry that changes where the stop sits relative to the glass. The batch carries such a
stop as the next surface's clear aperture instead. The opening is preserved exactly; the opaque
plate outside it is not modelled, so the record states `STOP_NOT_BLOCKING` and
`eligible_for_stop_dependent_evaluation: false` rather than claiming a plate it does not have.

**Derived apertures are bounded by the source's own radii.** A semi-diameter large enough to clear
the axial beam can still make two consecutive surfaces intersect, because the source's radii put the
second surface's edge in front of the first's. The batch caps the clear aperture at the largest
radius that keeps the glass positive, and records both the rule and the cap. The cap is a property
of the source's radii, not a choice about the design.


## Conversion method

Per input:

1. **Read the publication's surface table** for the example, verbatim. The table gives the surface
   number, the radius, the thickness, the index and the Abbe number.
2. **Confirm the example number.** The publication's own numbering is not the page order. Where the
   publication has a parameter summary with one row per example, a distinguishing value (a radius, a
   thickness, a condition value) maps the row to the surface table; where it has none, the record
   says the mapping is unconfirmed.
3. **Translate** into the surface table: `r` becomes `radius` with its sign, `d` becomes `thickness`,
   `nd` becomes a material id, and the stop row is marked.
4. **Import** with the engine's own importer. The importer does the element and stop decomposition
   and the edge and solid checks; the builder never second-guesses them.
5. **Assemble the Project** from the import result: the `COMPOUND` unit, its members, the materials,
   one source at the primary wavelength and one observation plane at the reported image plane.
6. **Check** with `project check` and `project solids`, and read the first-order values from
   `project paraxial`.

## The material decision this source forces

A patent table states `nd` and `vd`. **Two numbers do not define a dispersion curve.** The
LensLibrary batch could fit a three-term Sellmeier to each glass because the upstream prescription
dump prints the actual index of every surface at every enabled wavelength; a patent states no such
table.

Three routes were available and two were rejected:

- **Fit a Sellmeier to `nd` and `vd`.** Rejected: any curve through two points is an invention, and
  a reviewer cannot tell which invention produced the record.
- **Substitute a published catalog glass with the same `nd` and `vd`.** Rejected as a substitution.
  It was run as a cross-check: of the index pairs in this batch, a large share have no catalog glass
  within `|Δnd| ≤ 5e-5` and `|Δνd| ≤ 0.05` (for example `1.88815 / 40.76` and `1.97189 / 17.47`
  have none), and where a close glass exists the residual still moves the focal length by about
  0.1 %, which is outside the record format's paraxial tolerance. Substituting would have produced
  records that claim a colour correction the source never stated.
- **A constant index at the stated `nd`.** Adopted. It reproduces the source exactly at the d line
  and claims nothing about colour. The stated `vd` is kept in the record as `source_stated_vd` and
  is not used.

The consequence is stated in every record: `CONSTANT_INDEX_NO_DISPERSION` and `SINGLE_WAVELENGTH`.
A consumer that needs axial colour must not read these records as colour-corrected; a consumer that
needs the prescription at the d line gets it exactly.

## Paraxial verification, and why most records say `NOT_CHECKED`

The seed batch records `paraxial: MATCHED` because the upstream dump states an effective focal length
and a back focal length to four decimals, and the engine reproduces both. A patent does not.

Where a patent prints a normalised `f = 1.000` in a summary table, that number is a design target,
not a measured focal length: the engine lands within about 0.2 % of it, outside the format's
`1e-5 + 1e-4·scale` rule. The difference is not a conversion error and it is not the material model
(a constant-index build does not remove it); it is the source's own internal rounding between a
normalised target and a four-decimal surface table.

Records whose publication states no first-order value therefore carry:

- `validation.paraxial: NOT_CHECKED`, with the reason in `validation.notes`,
- the engine's own EFL and BFL under `first_order.engine_check` as `ENGINE_COMPUTED` facts,
- `comparison.verdict: NOT_APPLICABLE`, with `comparison.why` naming the missing value.

Records are not tuned to hit a stated number. Where a publication does state an EFL and a BFL in
millimetres, the comparison is run against the batch rule below and the verdict is `MATCHED` or
`DIFFERS`.

### The tolerance this batch applies

The format's default rule (`1e-5 + 1e-4·scale`) is below what a publication that prints a normalised
focal length and a four-decimal surface table can support, so this batch states the rule it applies,
as the acceptance clause requires:

| Quantity | Rule |
|---|---|
| every record | `\|engine − source\| ≤ 0.005 mm + 2e-3 · scale` |

- `atol = 0.005 mm` is half a unit in the last decimal of the coarsest focal length these
  publications print (`Focal length 0.35`). It is applied uniformly because a record does not carry
  its table's print precision as a machine-readable field; the cost is that a five-decimal print is
  held to a looser bound than its digits would allow, and the reviewer sees that in `atol_mm`.
- `rtol = 2e-3` is `1e-3` for a prescription printed to four decimals on radii as small as 0.27 mm,
  plus `1e-3` for the engine's air-referenced index convention measured below (0.06–0.1 % on EFL).

`comparison.rule`, `comparison.atol_mm` and `comparison.rtol` carry this rule in every record, so a
reviewer recomputes each verdict from the record alone. Where the two values still disagree outside
it, the record says `DIFFERS` and the batch defers it with `PARAXIAL_DIFFERS` rather than merging a
record that claims a first-order agreement it does not have. Four records are deferred that way: the
prescription reproduces the stated focal length but not the stated back focal length, by 0.02 mm to
2.6 mm, which is a disagreement about the image position and not about print precision.

The tighter rule stays available: at `1e-5 + 1e-4·scale` every record in this batch whose
publication states a focal length fails, because the residual is the engine convention plus the
source's own rounding rather than a transcription error. That is the upstream question this batch
raises instead of patching around, and the batch rule above is the honest bound until it is
answered.

### The engine's air-referenced index convention

Every record models a glass as a constant index at the patent's stated `nd`. A patent's `nd` is the
index **relative to air**; the engine reads a constant index as an **absolute** index and normalises
it by the design air index (`n_air = 1.000277` at the d line). The patent's `nd` therefore enters
the model `2.77e-4` low, and the paraxial focal length comes out about 0.06–0.1 % long.

Measured, not assumed: a plano-convex test lens of radius 10 mm and constant index 1.500000 returns
an EFL of 20.0164 mm instead of 20 mm. Rebuilding the same prescriptions with the index multiplied
by `n_air` returns the publication's own values — `us8164834b2-ex1` gives EFL 0.981299 mm and
back focus 2.021817 mm against the stated 0.981 and 2.022, and `us6844985b2-ex1` gives 1.303265 and
0.050234 against 1.303 and 0.050.

The batch follows the engine's convention rather than correcting each record, for two reasons:

- the offset is a property of the engine's material model, not of the prescription, so correcting it
  per record would encode an engine workaround into the corpus;
- a per-record correction would make the same glass carry two different indices inside one
  collection, and a consumer comparing two records could not tell which convention each used.

It is stated here, in the approximations and notes of the records where it affects a stated
comparison, and raised upstream rather than patched around.

## Verification run

Three outcomes are tracked separately per record, as the format requires:

| Outcome | Command | Required |
|---|---|---|
| Conversion | the engine's importer | `PASSED` |
| Native Project check | `os-optics project check` | `ACCEPTED` |
| Optical solids | `os-optics project solids` | `VALID` |
| Paraxial | `os-optics project paraxial` | `MATCHED` where a source value exists, else `NOT_CHECKED` |

Reviewer commands, from the repository root:

```
os-optics project check records/endoscopic-imaging/us11543647b2-ex1/project.json
os-optics project solids records/endoscopic-imaging/us11543647b2-ex1/project.json
os-optics project paraxial records/endoscopic-imaging/us11543647b2-ex1/project.json \
    --components LENS --wavelength-nm 587.7220645720764
```

## Known limits

- **No colour.** Every glass is a constant index at the d line. Axial colour, secondary spectrum and
  chromatic focal shift are not represented, and the records say so.
- **One state per record.** A focus-travel or zoom prescription is transcribed for one stated state,
  named in the record. The other states are not split out.
- **Derived clear apertures.** Where the publication states no clear aperture, the semi-diameters are
  derived from the stated F-number and field. They set the stop plate and the solid checks; they do
  not enter the first-order values.
- **No field verification.** No per-field configurations were built, so no field-dependent quantity
  (image height, CRA, distortion, edge MTF) is asserted.
- **No operating range claimed.** `spectrum.continuous_coverage_nm` is `UNCONFIRMED` on every record.
- **Field-convention mismatches are left open.** Where a publication's stated half angle and image
  height cannot both be true with the engine's focal length, the record says so in
  `field.convention_note` instead of choosing one.
- **Source assets stay upstream.** No patent PDF, figure or table image is committed; each record
  cites the publication number, the example and a SHA-256 of the transcribed table text.

# Camera search evidence

This note describes condition-specific facts for camera lens Projects. The preserved
[Yutong Cup problem 2 acceptance case](../acceptance/yutong-2025-mirrorless-problem-2.json)
exercises the frozen [P2b record](../records/yutong-cup/yutong-2025-mirrorless-18-p2b/record.json)
from the first camera intake. Production retrieval uses [Project facts](RECORD_FORMAT.md#what-retrieval-uses)
under a single applicable configuration. A competition target or published specification remains
intake context until the committed Project supports the corresponding fact.

| Fact | Comparable conditions |
| --- | --- |
| Geometric back focus | Use `first_order.final_powered_vertex_to_sensor_mm` with `distance_kind: geometric_axial_distance`, `from: last_powered_surface_vertex` and `to: fixed_image_plane`. The `includes_cover_glass` flag and `image_format.cover_glass` identify the plate included in the distance. `first_order.back_focal_length_mm` can also carry endpoints for its paraxial focal distance. |
| MTF over a field interval | In `performance`, `mtf_field_minimum` gives the minimum over the declared frequency, spectral weights, field mapping, samples and orientations at a fixed image plane. For this case, the field coordinate is real image height divided by the diagonal image radius. `sampled_field_fractions` gives the sampled positions, including the center, interior positions and the 0.8 endpoint. `limiting_sample` identifies the lowest point and orientation. |
| Corner relative illumination | In `performance`, use `relative_illumination` with the image-circle diameter, normalized field position and mapping, center normalization, scene radiance, transmission model and spectral sampling. `design_coatings` marks the modeled coated result and `uniform` marks a geometric throughput calculation. |
| CRA and distortion | Record the sampled field bounds and mapping. CRA requires an angle reference and the wavelength aggregation. Distortion requires the reference projection, wavelength and signed minimum and maximum. |
| Sensor, spectral sampling and cost | `pixel_pitch_um` is the horizontal and vertical pitch in micrometers. Keep discrete wavelength and weight arrays together. Compare proportional weight vectors for the same ordered wavelengths. The span of the samples expresses a search interval. Compare `cost_proxy` only for the same element weights and scope. The contest gives spherical and aspheric powered glass elements weights of one and three and prefers lower cost. |

The camera records use the [shared record format](RECORD_FORMAT.md). A native iris
specifies its opening diameter as the system aperture. The separate
`aperture.entrance_pupil_f_number` fact describes the f-number from EFL and the
entrance pupil with its measurement convention. The case requires a blocking
stop and comparable pupil-based evidence for f-number, MTF and relative
illumination.

`field.verification` points to the original fixed-plane chief-ray results and
records the largest difference from the intended field heights. A reported
image-circle diameter carries the meaning of the tested field. The Sonnar's
largest verified image height is 21.63 mm, which gives a tested diameter of
43.26 mm for a nominal 36 × 24 mm frame.

The MTF result is a sampled minimum. The P2b record covers 11 field heights
through 0.8 at 40 lp/mm in both orientations. Its value of 0.722847 describes
the tested set. A query for a field interval needs a field sweep with a stated
sampling basis. This fixture pins the 11 ordered field fractions and requires
a resolvable evidence source for the interval MTF and corner illumination.
An isolated point or a sparser sweep leaves the interval unconfirmed.

## Analysis entries and source locators

- `analysis_evidence.tolerance` states the method and simulation or measurement
  status. Gate results include sample and pass counts, conditions and source
  locators. Keep physical lot yield separately.
- `analysis_evidence.ghost` lists sourced methods with their scope, assumptions
  and results. Trace outcome and omitted-power bounds remain separate from
  whether the study exists. Problem 2 requires both studies and sets no
  numerical gate for either. The P2b native ghost trace is partial and the
  two complementary screens cover their stated modeled paths.
- The two existing camera records link to compact `evidence.json` extracts.
  A fact's `source` identifies the repository-relative file `path`, its SHA-256
  over committed bytes and the entry under `sections`. For retrieval, use the
  results that apply to the Project. Return only the needed conditions and
  outcomes by default. Expanded evidence and source locators are available
  through explicit inspection.
- The [P2b extract](../records/yutong-cup/yutong-2025-mirrorless-18-p2b/evidence.json)
  provides the sampled field and radiometry table and the modeled tolerance
  and ghost summaries. The [Sonnar extract](../records/opensci-design-studies/portrait-85-sonnar-eight/evidence.json)
  provides its field and MTF summaries. The original detailed simulations
  remain in the upstream studies.
- Both original contributed camera Projects and the accompanying evidence
  extracts use [MIT](../LICENSE). The records cite the starting patent
  publications as provenance.

## Run the acceptance case

```console
python -m unittest discover -s tests -v
```

The fixture defines 16 checks and one ranking preference. It uses the P2b
infinity-focus configuration for the exercise. The test runs
them against the frozen record and exercises counterexamples for wrong distance
endpoints, sparse field sampling, strict MTF thresholds, mismatched
corner illumination, a nonblocking stop and missing or unresolved evidence. A missing or inapplicable
fact is `UNCONFIRMED`. A comparable value outside the requested limits is
`DOES_NOT_MATCH`. The test runs directly on the record and is the reference
for a future index-backed implementation.

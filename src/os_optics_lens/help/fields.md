# Fields (typed facts)

A fact is one quantity at one location of a configuration view, named by a dotted key:

* `<section>.<member>` for first_order, aperture, field, image_format, design, cost_proxy and
  future sections, e.g. `first_order.effective_focal_length_mm`, `aperture.f_number`,
  `field.half_field_deg`, `image_format.image_circle_diameter_mm`,
  `design.powered_element_count`, `cost_proxy.weighted_spherical_element_count`;
* `spectrum.sampling_span_nm` (discovery only), `spectrum.continuous_coverage_nm` (interval with
  a state), `spectrum.sampling_wavelengths_nm`, `spectrum.primary_wavelength_nm`;
* `performance.<metric>` per performance entry, e.g. `performance.mtf_field_minimum`,
  `performance.relative_illumination`, `performance.cra`, `performance.distortion_range`;
* `analysis_evidence.tolerance`, `analysis_evidence.ghost` (one fact per study).

Units come from the key suffix: `_mm` mm, `_deg` deg, `_nm` nm, `_um` um, `_percent` percent,
`_lp_mm` lp/mm; f-numbers, numerical apertures, counts, fractions and weights are `1`.

## Listing and describing

```
os-optics-lens fields                                   every key in scope (limit 100, more)
os-optics-lens fields --filter design_type=camera_lens --prefix performance.
os-optics-lens fields --fact aperture.entrance_pupil_f_number
```

Per key: `records` (count in scope), `unit`, `value_type`, `bases`, `condition_variants`,
`conditions_keys`, `min`, `max`, `stop_dependent`.

`--fact KEY` adds `value_paths` (numeric members, e.g. sagittal/tangential), `applicable_ops`,
`eligible_for` (DISCOVERY_ONLY marks discovery facts), `constraint_form` ({scope, metric, path}),
up to 10 `variants` of conditions/qualifiers/sampling grids with counts, and `example_constraint`
(with `required_sampled_field_fractions` for a sampled fact): a valid
constraint (or `example_filter` for discovery-only facts) you can paste into `constraints` and
tighten. Its numeric value spans the most common variant's min..max, so narrow it to your brief.

## From a field to a constraint

1. `fields --fact performance.mtf_field_minimum` shows the conditions variant, e.g.
   `{"frequency_lp_mm": 40, "field_end": 0.8, "spectrum": {...}, ...}`.
2. Copy the conditions your brief fixes into `conditions`; omitted keys are reported as
   `unpinned` on a MATCH.
3. Set `op`/`value`:

```
{"fact": "performance.mtf_field_minimum", "op": "gt", "value": 0.3,
 "conditions": {"frequency_lp_mm": 40, "field_basis": "diagonal_image_height_fraction",
                "field_start": 0, "field_end": 0.8},
 "requires_evidence_source": true}
```

Interval facts (min_/max_ members, e.g. `performance.distortion_range`) need `path`
(`min_percent` or `max_percent`). `spectrum.continuous_coverage_nm` takes `covers_interval`.

## Basis and conditions

`basis` is the fact's own basis or the nearest applicable one. Its class can be
`PROJECT_DECLARED`, `PROJECT_DERIVED`, `ENGINE_COMPUTED`, `DERIVED` or `OTHER`. Structured
`conditions` are comparison keys. Prose conditions (`conditions_text`) describe the value. `compare` marks rows whose units,
conditions, qualifiers or sampling grids differ as `comparable: false` with `differs_by`.
Field discovery uses the Project's own optical values and applicable analysis results.
Use `inspect ID --provenance` for the record's direct source and attribution.

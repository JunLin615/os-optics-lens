# Facets

Facets narrow the collection for discovery; they never decide a constraint outcome. Values are
canonical lower_snake_case terms. `os-optics-lens facets` lists the values present with record
counts; `--describe` adds these descriptions. Filters match terms case-insensitively after
replacing spaces and hyphens with underscores, and accept aliases.

| facet | level | source and meaning |
|---|---|---|
| `collection` | record | collection.collection_id |
| `family` | record | design.design_family_id: meaningful variants of one design |
| `design_scope` | record | complete_system, lens_group or singlet |
| `design_type` | record | kind of prescription, mapped from design.lens_type (camera_lens, microscope_objective, endoscope_objective, ...) |
| `source_lens_type` | record | the source's own lens type label, verbatim |
| `function` | record | what the prescription does to light (imaging, illumination, collimation, ...) |
| `application` | record | documented use (photography, microscopy, endoscopy, mobile_imaging, ...) |
| `structure` | record | layout or property (aspheric, cemented_group, double_gauss, refractive, ...) |
| `band` | configuration | spectral band of the sampled wavelengths; discovery only, never coverage |
| `image_format` | configuration | nominal format (full_frame, aps_c, ...); use image_format dimensions and the image circle for coverage |
| `conjugate` | configuration | infinite_to_finite, finite_to_finite, ... |
| `field_kind` | configuration | how the design states its field: angular, object height or image height |
| `system_aperture` | configuration | the aperture fact the design file declares (f_number, entrance_pupil_diameter_mm, ...) |
| `module_role` | record | where a phone module sits in its device (design.module_role): main_camera, ultrawide, telephoto, front_camera; not what the prescription is. Another record value is indexed verbatim (UNKNOWN_TERM) and filters match it exactly |
| `stop_state` | record | stop_blocking, stop_not_blocking or no_stop (design.aperture_validity.state) |
| `evidence` | configuration | performance metrics and studies present (mtf, mtf_field_minimum, cra, relative_illumination, tolerance_study, ...) |

Configuration-level facets are checked per configuration; record-level facets apply to every
configuration of the record.

## Aliases

| alias | facet | term |
|---|---|---|
| smartphone, phone, mobile, cell_phone | application | mobile_imaging |
| camera, photographic_lens | design_type | camera_lens |
| objective | design_type | ambiguous: microscope_objective or endoscope_objective (AMBIGUOUS_TERM) |
| ff, 35mm_full_frame | image_format | full_frame |
| apsc | image_format | aps_c |
| vis, nir, uv, mwir, lwir | band | visible, near_infrared, ultraviolet, mid_wave_infrared, long_wave_infrared |
| infinity | conjugate | infinite_to_finite |
| main, ultra_wide, tele, front | module_role | main_camera, ultrawide, telephoto, front_camera |

A vocabulary term with no records in the index is not an error: the result is empty with warning
`FACET_VALUE_EMPTY`. A value that is neither a term nor present is `UNKNOWN_FACET_VALUE` with
close suggestions. Uncontrolled facets (collection, family, source_lens_type, system_aperture,
evidence) match the indexed values verbatim.

## Band boundaries (vacuum nm)

| band | interval |
|---|---|
| extreme_ultraviolet | 0.1 - 10 |
| ultraviolet | 10 - 380 |
| visible | 380 - 780 |
| near_infrared | 780 - 3000 |
| mid_wave_infrared | 3000 - 8000 |
| long_wave_infrared | 8000 - 15000 |

Examples:

```
os-optics-lens facets --facet design_type --facet structure
os-optics-lens facets --filter design_type=camera_lens --filter image_format=ff --facet family
os-optics-lens search --mode starting_point --filter application=smartphone --prefer first_order.total_track_mm
os-optics-lens search --mode starting_point --filter module_role=tele --target first_order.effective_focal_length_mm=7~1.5
```

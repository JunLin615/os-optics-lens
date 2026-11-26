# Quickstart: from a design brief to an editable Project

`os-optics-lens` searches an indexed collection of optical prescriptions locally and hands off
the native `os-optics` Project of the record you choose. Every command prints one JSON document
(`{"ok": true, ...}` or `{"ok": false, "error": {...}}` with a non-zero exit code). Output is
compact and bounded; ask for details explicitly.

The index is the wheel's bundled index unless `--index FILE` or `OS_OPTICS_LENS_INDEX` names
another. `os-optics-lens info` shows which index answers and its `snapshot.id`.

## 1. See what the collection holds: facets

```
os-optics-lens facets
os-optics-lens facets --facet design_type --facet image_format --facet band
os-optics-lens facets --filter design_type=camera_lens --describe
```

Facet values are canonical terms with record counts. Filters accept aliases (`ff`, `smartphone`,
`nir`, ...). Repeating `--filter` for one facet ORs the values; different facets AND.
See `os-optics-lens help facets`.

## 2. Find the quantities you can constrain: fields

```
os-optics-lens fields --filter design_type=camera_lens
os-optics-lens fields --fact image_format.image_circle_diameter_mm
os-optics-lens fields --fact performance.mtf_field_minimum
```

`fields` lists fact keys with unit, value type, bases, condition variants and min/max.
`fields --fact KEY` adds the applicable operators, the distinct conditions and a ready-to-run
`example_constraint`. Copy and tighten it. See `os-optics-lens help fields`.

## 3. Search

Specification search: hard constraints only; results are MATCH, UNCONFIRMED or DOES_NOT_MATCH.

```
os-optics-lens search --mode specification --filter design_type=camera_lens \
  --where 'first_order.effective_focal_length_mm between_inclusive [17.1,18.9]' \
  --where 'image_format.image_circle_diameter_mm gte 43.27'
```

Starting-point search: flexible targets ranked by normalized deviation, with hard constraints
optional.

```
os-optics-lens search --mode starting_point --filter design_type=camera_lens \
  --target first_order.effective_focal_length_mm=24~3 --prefer design.powered_element_count:asc
```

For anything beyond quick flags, write the request as JSON and pass `--request FILE`
(`os-optics-lens help request`). On Windows PowerShell always use `--request FILE`: the shell
strips the double quotes from inline JSON.

Read each candidate's `outcome`, `constraints`, `shortfalls` and `unknowns`
(`os-optics-lens help response`, `os-optics-lens help semantics`). If `page.next_cursor` is not
null, repeat the same request with `--cursor <next_cursor>` for the next page.

## 4. Compare a short list

```
os-optics-lens compare lenslibrary-528155 yutong-2025-mirrorless-18-p2b \
  --fields first_order.effective_focal_length_mm,aperture.f_number
```

Rows with `comparable: false` name what differs (`differs_by`: unit, conditions, qualifiers,
sampled_field_fractions).

## 5. Inspect one record

```
os-optics-lens inspect yutong-2025-mirrorless-18-p2b
os-optics-lens inspect yutong-2025-mirrorless-18-p2b --fields performance.mtf_field_minimum
os-optics-lens inspect yutong-2025-mirrorless-18-p2b --evidence all --max-output-bytes 65536
os-optics-lens inspect lenslibrary-528155 --prescription summary
os-optics-lens inspect lenslibrary-528155 --provenance
```

Ordinary inspection shows Project identity, configuration, available fact keys and supporting
evidence section names. Use `--evidence SECTION` to read an analysis or `--provenance` to read
the record's direct source, attribution and redistribution terms.

## 6. Hand off the native Project

```
os-optics-lens project yutong-2025-mirrorless-18-p2b
os-optics-lens project yutong-2025-mirrorless-18-p2b --out work/p2b.project.json
```

`--out` writes the exact bytes (its `written_sha256` equals the record's `native_project.sha256`)
and refuses to overwrite without `--force`. Open, check and evaluate the file with `os-optics`;
the search result is evidence for choosing it, not a replacement for evaluating it against your
brief.

## Other topics

`os-optics-lens help request | response | semantics | errors | facets | fields`

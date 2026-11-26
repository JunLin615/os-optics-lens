# Prescription library architecture

An agent searches by optical requirements, inspects a small set of candidates, then retrieves a native `os-optics` Project to open, check, edit or evaluate. The library also supports finding a reference structure, such as a lens group to adapt within a different design. The repository layout, index, retrieval interface and documentation serve these agent workflows.

## Records and index

- Keep one native Project per design record and compact metadata that helps a caller choose or use it. Give each record a stable ID, revision and design family where known. Preserve the native Project's configurations, materials and surface models.
- Build the search projection from an explicit set of Project facts. Use native values, calculations from the committed Project and analyses applicable to that revision. Index them against their configuration with the relevant units and conditions. A query with several hard constraints must find them together in the same configuration.
- Keep the contributor's direct-source reference, attribution and redistribution basis available through explicit provenance inspection. Source optical values, source comparisons and intake reports stay outside the search projection. Existing records may carry those fields as review data.
- Generate the search index from independently editable records. Bundle the index, collection and retriever in the first usable PyPI release for local queries after installation. An embedded SQLite index is the initial storage option for facets, ranges and sorting. Keep the source records independent of the index backend.

## Classification and search fields

Use independent facets to narrow candidates and typed fields to compare them. A prescription may have multiple optical functions, applications and structural traits. Keep family membership separate so an agent can search across families or inspect variants of one design.

Assign a controlled term when a source statement, native Project property or documented contributor observation supports it. Keep optional facets sparse. Prefer a query alias when a new phrase means the same thing as an existing term. Add a term when it identifies a useful group of records or helps an agent choose the right search fields. See [Optical search vocabulary](SEARCH_VOCABULARY.md) for first-pass terms, candidate terms and source mappings.

| Area | Search fields and interpretation |
| --- | --- |
| Design scope | Complete system, lens group or singlet. This lets a system search and a component search use the same collection. |
| Spectral band | Use sampling wavelengths to derive discovery facets. Store confirmed continuous coverage of the Project separately and use it for hard band constraints. |
| Image format | Full frame and APS-C are discovery facets. Use the Project's established image circle or image dimensions for coverage checks. Label an estimated image circle as a discovery fact. A 36 × 24 mm frame requires about a 43.3 mm image circle for geometric coverage. Use field-dependent results to assess edge image quality. |
| Optical function | The task performed, such as imaging, illumination, collimation or spectral dispersion. |
| Design type | The kind of prescription, such as a microscope objective, projection lens, condenser or spectrometer. |
| Application | A documented use, such as microscopy, machine vision, endoscopy or projection display. |
| Structure | A supported layout or optical property, such as zoom, catadioptric, cemented or image-space telecentric. |
| First-order properties and size | Record effective focal length, back focal length, f-number, clear aperture and total length with units, definitions and applicable configuration. Identify the conjugate and f-number convention where relevant. State the optical axis, measurement endpoints and convention for back focus and total length, including whether a specified cover plate is in the measured working distance. |
| MTF | Record spatial frequency and units, field position, wavelength or spectral weighting, and sagittal or tangential direction where applicable. Compare values under matching conditions. For a field-interval minimum, record the field coordinate, bounds, sampled positions and included orientations. |
| Chief ray angle and distortion | For CRA, record the image-plane field position and angle convention. For distortion, record the field position, the reference projection and the sign or absolute-value convention. |
| Relative illumination | Record image-plane position and field mapping, the image-circle diameter, reference irradiance, source and transmission assumptions, and the spectral conditions. |
| Analysis evidence | Index the availability and useful results of tolerance and ghost studies that apply to the Project revision. Preserve method and scope. Show supporting calculations on request. |

Prioritize wavelength range and configuration, with image circle, effective focal length, f-number, total length and back focal length for the initial imaging records. Expose optional task-specific fields where records support them. For example, a microscope objective can supply object-side NA and working distance. Illumination can supply source and target conditions and output distribution. Attach each value to its configuration and, where relevant, its optical path or output. Keep missing or inapplicable values explicit when requested. Use MTF, CRA, distortion and other condition-specific quantities for general ranking once the collection contains comparable data.

The [camera evidence notes](docs/CAMERA_SEARCH_EVIDENCE.md) describe the Project facts available for camera queries and the historical Yutong Cup fixture.

## Collection scope

- Start with complete optical systems, alongside a small set of representative singlets and lens groups with clear design reuse value. A singlet may contribute a useful aspheric form or a documented operating condition. A cemented doublet may provide a material pairing, interface geometry and correction strategy that another design can reuse.
- Treat focal length as a search value. Choose representative structures and meaningful variants for the library. Route selection by stock focal length, diameter or part number to the optical component catalog.
- Preserve coatings that are part of a design as native surface properties. Record wavelength, incidence angle, polarization and source where needed to interpret their optical effect. Catalog part selection remains the route for purchasing finished coated optics.
- Index the properties each record can support. Image circle, CRA and edge MTF require a defined image plane and field conditions. A group or singlet with no such conditions can still be found by structure and the first-order properties it supplies.

## Retrieval behavior

- For discovery, expose a bounded list of facet values present in the current collection or query scope, with record counts. After a type or function is selected, surface the applicable numerical fields and relevant conditions. Keep the full vocabulary and detailed field definitions available on demand.
- Resolve aliases to canonical terms. Intersect filters across facets and allow the caller to request alternatives within a facet. Check each label and numerical fact at the scope where it applies. For a projector with an illuminator and an imaging path, an illumination uniformity constraint applies to the requested illumination output.
- **Specification search** applies the requested constraints to comparable Project values. Return each requested result with the conditions needed to interpret it. A fact without a comparable Project value is unconfirmed for that constraint.
- **Starting-point search** ranks Projects against the caller's flexible targets and preferences. A 27 mm design can be a useful candidate for a 24 mm brief if its image circle and back focus are close and focal length is flexible.
- Return a bounded shortlist with record IDs, revisions, configurations and the facts that affect this query. Omit unrelated gaps. Comparison, ordinary inspection and Project handoff follow the same scope. Expanded inspection can show selected Project facts, surfaces and supporting calculations. Provenance and any retained transcription detail require an explicit request.
- Let the agent fetch an editable Project by ID and revision and make family variants available on demand. Bound total batch output and make additional results available from the same index snapshot.

This document establishes the architecture and field semantics. Record layouts, exact band boundaries, database schema and command syntax are implementation decisions to be documented with the first usable package.

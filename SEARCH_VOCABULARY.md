# Optical search vocabulary

Use controlled English terms as optional facets on the same native records. The terms below are for mapping and review. A released index advertises terms represented by accepted records. Candidate terms stay in this reference until use in the collection warrants them.

## First-pass terms

| Facet | Terms to consider first | What the facet describes |
| --- | --- | --- |
| Optical function | Imaging, illumination, collimation, beam shaping, spectral dispersion, optical coupling | What the prescription does to light or produces at its output. A combined design can have more than one function. |
| Design type | Camera lens, microscope objective, projection lens, telescope, spectrometer, condenser, collector, relay lens, collimator, beam expander | What kind of optical system or group an agent can retrieve. |
| Application | Photography, mobile imaging, microscopy, endoscopy, projection display, machine vision, spectroscopy, astronomy | Where the source or contributor documents using the design. |
| Structure | Fixed focal length, zoom, refractive, reflective, catadioptric, folded, cemented group, aspheric, object-space telecentric, image-space telecentric | An optical layout or property supported by the source or native Project. |

Design scope, spectral band and image format remain separate facets. Give each published term one canonical spelling. Resolve synonyms at query intake. For example, `cell phone` and `smartphone` can resolve to `mobile imaging`. An ambiguous phrase such as `objective` needs a more specific facet value. Keep a source's terminology and classification basis in provenance when they explain the mapping.

## Terms to consider as coverage grows

These terms describe possible search tasks. Use them when records provide a meaningful distinction for retrieval and the term can be assigned consistently.

| Search area | Candidate terms and placement |
| --- | --- |
| Microscopy and medical imaging | Tube lens, endoscope objective and eyepiece as design types. Fluorescence microscopy, confocal microscopy and stereomicroscopy as applications. Finite conjugate and infinity corrected as optical properties. |
| Illumination | Illumination homogenizer and light pipe as design types. Microlens array, Köhler illumination and critical illumination as structural or layout descriptions. LED lighting, display backlight and structured-light projection as applications. |
| Laser and coupling | Beam scanning as a function, F-theta scan lens and fiber coupler as design types, and laser processing as an application. Afocal and anamorphic as optical properties. |
| Spectroscopy | Czerny–Turner and Dyson as spectrometer architectures. Echelle, grating-based and prism-based as structural descriptions. |
| Imaging and viewing | Macro lens, fisheye lens and head-up display optics as design types. Remote sensing, thermal imaging, near-eye display and microlithography as applications. Cooke triplet, Tessar and Double Gauss as architecture terms when supported by the source. Use a separate family ID for variants of a particular prescription. |
| Mobile modules | `main_camera`, `ultrawide`, `telephoto` and `front_camera` as a module-role facet on phone prescriptions. The role says where the module sits in a device, not what the prescription is: the same camera-lens structure can serve any of them, so keep it separate from design type and application. Assign it only when the source or the contributor establishes the role. |

## Search facts by task

Shared fields such as wavelength and clear aperture can be used across tasks. Index the values of the committed Project, including analyses of that Project, with their configuration, units and relevant conditions. A source's printed values and transcription comparisons belong to intake. Classification may use the contributor's direct-source description.

| Agent task | Useful search facts |
| --- | --- |
| Camera or projection imaging | Effective focal length, f-number, image circle or image dimensions, total length, back focal length and conjugates. Use condition-specific MTF, chief ray angle, distortion or relative illumination when available. See the [camera search evidence proposal](docs/CAMERA_SEARCH_EVIDENCE.md) for field-interval, radiometry and analysis evidence semantics. |
| Microscope objective | Object-side numerical aperture, working distance and its reference, magnification and tube-lens basis, object-space field, immersion medium, cover-glass thickness and conjugate. |
| Illumination or collection | Source size and angular distribution or collection NA, target dimensions and distance, and output angular distribution. State the source, target region, definition and normalization for reported uniformity or efficiency. |
| Beam expansion, scanning or coupling | Beam expansion ratio and beam diameter convention, scan field and spot criterion, or fiber type, mode-field diameter or core diameter and NA as applicable. Attach coupling efficiency to the input beam, fiber and alignment conditions. |
| Spectrometer | Wavelength range, slit width, detector dimensions and sampling, and the wavelength and criterion for resolution. Keep wavelength dispersion separate from spectral resolution. |
| Viewing or display | Field of view and eye relief where applicable. For a head-up or near-eye display, use eyebox dimensions and virtual image distance where defined. |

A label helps find a candidate. A hard numerical filter requires a comparable value under the requested conditions. For example, `microscopy` can retrieve both an objective and a condenser. Adding `microscope objective` selects the imaging group, and an object-side NA filter can then test the requested aperture. If the record has only an object-space field diameter, report that fact in its original units and meaning.

## LensLibrary mapping examples

- `camera_cooke` and `camera_tessar` map to the imaging function and camera-lens type. The source supplies an architecture hint. Use family IDs for documented relationships between specific designs or variants.
- `microscope_objective` maps to imaging, microscope objective and microscopy. Keep object-side NA and object-space field in millimeters as distinct Project facts when established.
- `projection_lens_zoom` maps to imaging, projection lens and zoom. Add the projection-display application if the source establishes it. A complete projector that also contains its illuminator can carry both imaging and illumination.
- `spectrometer_CZT` maps to spectral dispersion and spectrometer. Preserve the source's Czerny–Turner designation for finer retrieval when that term is enabled.

The mapping applies to accepted native Projects. A source label alone neither establishes conversion fidelity nor provides a numerical result. [Pinned LensLibrary property list](https://github.com/nzhagen/LensLibrary/blob/86b2c0cac5a67e2112d58518230c4d8728154f4e/lens_properties_list.txt)

## References used to curate the vocabulary

- Patent classification supplies names for optical layouts and instruments. The library's facets are organized around retrieval tasks. [USPTO Cooperative Patent Classification G02B](https://www.uspto.gov/web/patents/classification/cpc/html/cpc-G02B.html)
- Objective markings and microscope alignment explain why objective conditions differ from condenser conditions. [Nikon objective specifications](https://www.microscopyu.com/microscopy-basics/microscope-objective-specifications) · [Nikon Köhler illumination](https://www.microscopyu.com/tutorials/kohler)
- Application references show which inputs make illumination, spectrometer and display performance comparable. [Ansys illumination systems](https://optics.ansys.com/hc/en-us/articles/42661742770579-Introduction-to-illumination-systems) · [Ansys spectrometer design](https://optics.ansys.com/hc/en-us/articles/42661659061779-How-to-build-a-spectrometer-theory) · [Ansys head-up display](https://optics.ansys.com/hc/en-us/articles/10072384186899-Head-Up-Display-HUD)
- Optical design notes cover telecentric layouts and beam-expansion systems. [Edmund Optics telecentric design](https://www.edmundoptics.com/knowledge-center/application-notes/imaging/telecentric-design-topics) · [Edmund Optics beam expanders](https://www.edmundoptics.com/knowledge-center/application-notes/lasers/beam-expanders/)

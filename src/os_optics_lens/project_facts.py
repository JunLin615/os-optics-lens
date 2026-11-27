"""Project facts used by both the index and configuration evaluation."""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from typing import Any

from .canonical import is_number
from .normalize import FROM_SUFFIX, member_stem

PROJECT_BASES = {"PROJECT_DECLARED", "PROJECT_DERIVED", "ENGINE_COMPUTED"}
DESIGN_CLASSIFICATION = (
    "lens_type", "design_family_id", "design_scope", "function_tags", "application_tags",
    "structure_tags", "module_role", "conjugate", "folded", "focus",
)
CAMERA_COLLECTIONS = {"yutong-cup", "opensci-design-studies"}
PHONE_COLLECTIONS = {"phone-modules", "phone-telephoto"}
PARAXIAL_CHECK_COLLECTIONS = {"lenslibrary", *PHONE_COLLECTIONS}
CHECK_MEMBERS = {
    "engine_check", "comparison", "catalog_comparison", "catalog_comparisons", "verification",
    "prescription_dump_check", "property_list_comparison", "validation", "conversion",
    "intake", "checks", "optical_validation", "opening_outcome", "tolerance_mm",
    "absolute_difference_mm", "fraction_of_tolerance", "within_tolerance", "upstream",
}
BANDS = (
    ("extreme_ultraviolet", 0.1, 10), ("ultraviolet", 10, 380), ("visible", 380, 780),
    ("near_infrared", 780, 3000), ("mid_wave_infrared", 3000, 8000),
    ("long_wave_infrared", 8000, 15000),
)


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _objects(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _pick(obj: Any, *keys: str) -> dict[str, Any]:
    body = _object(obj)
    return {key: body[key] for key in keys if key in body}


_BASIS_TOKEN = re.compile(r"[A-Za-z_]+")


def _basis(value: Any) -> str:
    """The leading basis token (`SOURCE_STATED; brief` -> `SOURCE_STATED`). Admission compares it
    against exact upper-case allow-lists, so `source_stated` or `VENDOR_STATED` is never admitted."""
    match = _BASIS_TOKEN.match(value.strip()) if isinstance(value, str) else None
    return match.group(0) if match else ""


ANALYSIS_BASES = {*PROJECT_BASES, "DERIVED"}
"""Bases a value or object nested in an admitted legacy camera analysis may carry; elsewhere only
`PROJECT_BASES` are admitted, as at section level."""

CONVENTION_MEMBERS = frozenset({"field_basis", "scale_basis"})
"""Documented `*_basis` members that state a coordinate or scale convention, never a value's basis."""

_BASIS_CLASS = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*")


def _class_marker(marker_value: Any) -> bool:
    """Whether a marker states a basis class (`SOURCE_STATED`, `VENDOR_STATED`, `DERIVED`, ...)."""
    return isinstance(marker_value, str) and _BASIS_CLASS.fullmatch(_basis(marker_value)) is not None


def _qualifies(marker: str, marker_value: Any, key: str) -> bool:
    """Whether `marker` (`<name>_basis`) qualifies the sibling value `key`. A marker stating a basis
    class qualifies `name` and any `name_...` member, ignoring case (`quoted_mtf_basis` marks
    `quoted_mtf_ratio` and `Quoted_MTF`); a prose marker qualifies the names `_marker_names` gives."""
    if marker in CONVENTION_MEMBERS or key == marker or key.endswith("_basis"):
        return False
    name = marker[: -len("_basis")]
    if name in _marker_names(key):
        return True
    lowered, stem = key.lower(), name.lower()
    return _class_marker(marker_value) and (lowered == stem or lowered.startswith(stem + "_"))


def _marker_names(key: str) -> tuple[str, ...]:
    """The names a `<name>_basis` marker may use for value `key`: the key itself, the key without a
    trailing `_from_<word>`, and its stem without the unit suffix, as `normalize.member_stem` pairs
    them (`vendor_mtf_basis` marks `vendor_mtf_from_datasheet`, `width_basis` marks `width_mm`)."""
    return tuple(dict.fromkeys((key, FROM_SUFFIX.sub("", key), member_stem(key))))


def _marker(obj: dict[str, Any], key: str) -> tuple[bool, Any]:
    """The `<name>_basis` marker beside a value (see `_qualifies`); the most specific name wins."""
    markers = [m for m in obj if isinstance(m, str) and m.endswith("_basis") and _qualifies(m, obj[m], key)]
    if not markers:
        return False, None
    return True, obj[max(markers, key=len)]


def _pairs_a_value(obj: dict[str, Any], marker: str) -> bool:
    """Whether `<name>_basis` sits beside a value it qualifies. A `*_basis` member with no such
    value, or a documented convention such as `field_basis`, is an ordinary member."""
    return any(isinstance(key, str) and _qualifies(marker, obj[marker], key) for key in obj)


def _dropped_marker(obj: dict[str, Any], key: str, admitted: set[str]) -> bool:
    """A `*_basis` member leaves the projection when it states SOURCE_STATED, or when it qualifies a
    sibling value with a basis outside `admitted` (it leaves together with that value)."""
    if not key.endswith("_basis"):
        return False
    basis = _basis(obj[key])
    return basis == "SOURCE_STATED" or (basis not in admitted and _pairs_a_value(obj, key))


def _marked_outside(obj: dict[str, Any], key: str, admitted: set[str]) -> bool:
    """Whether a value is paired with a marker whose basis is not admitted, so the value is a
    source-stated or unclassified quantity and the value/marker pair leaves the projection."""
    present, marker = _marker(obj, key)
    return present and _basis(marker) not in admitted


def analysis_applies(document: dict[str, Any], project_sha: str | None, configuration_id: str | None,
                     revision: int) -> bool:
    """Check the applicability bindings an evidence extract declares."""
    return (document.get("native_project_sha256", project_sha) == project_sha
            and document.get("configuration_id", configuration_id) == configuration_id
            and document.get("record_revision", revision) == revision)


def _applicable(item: Any, applies: Callable[[dict[str, Any]], bool],
                admitted: set[str] = ANALYSIS_BASES) -> bool:
    """A nested object (or an `analysis_evidence` study) is kept when it states no basis, so it is
    part of the analysis that holds it, or states a basis in `ANALYSIS_BASES`; any other basis
    (SOURCE_STATED in any spelling, a vendor or contributor basis) removes it."""
    obj = _object(item)
    ref = obj.get("source")
    return ((obj.get("basis") is None or _basis(obj["basis"]) in admitted)
            and (not isinstance(ref, dict) or applies(ref)))


def _clean(value: Any, applies: Callable[[dict[str, Any]], bool],
           admitted: set[str] = ANALYSIS_BASES) -> Any:
    """Copy a Project analysis, dropping review members and keeping typed calculation references.
    `admitted` is the set of bases a nested object or a value marker may state."""
    if isinstance(value, list):
        return [_clean(item, applies, admitted) for item in value if _applicable(item, applies, admitted)]
    if not isinstance(value, dict):
        return value
    clean: dict[str, Any] = {}
    for key, item in value.items():
        if _marked_outside(value, key, admitted):
            continue
        if key == "source":
            if isinstance(item, dict):
                clean[key] = {k: item.get(k) if isinstance(item.get(k), str) else None
                              for k in ("path", "sha256", "section")}
        elif (key not in CHECK_MEMBERS and not key.startswith(("source_", "stated_", "printed_"))
              and not key.endswith(("_source", "_comparison", "_from_dump", "_air_nm"))
              and not _dropped_marker(value, key, admitted)
              and _applicable(item, applies, admitted)):
            clean[key] = _clean(item, applies, admitted)
    return clean


def _analysis_supported(item: Any, applies: Callable[[dict[str, Any]], bool], legacy_analysis: bool) -> bool:
    obj = _object(item)
    basis = _basis(obj.get("basis"))
    return _applicable(obj, applies) and (basis in PROJECT_BASES or (basis == "DERIVED" and legacy_analysis))


def _fact_section(section: dict[str, Any], applies: Callable[[dict[str, Any]], bool], *,
                  legacy_analysis: bool = False, admitted: set[str] = PROJECT_BASES) -> dict[str, Any]:
    """Admit explicitly Project-based facts and recognized legacy analyses."""
    whole = _analysis_supported(section, applies, legacy_analysis)
    facts = {}
    for key, value in _clean(section, applies, admitted).items():
        original = section[key]
        if isinstance(original, dict) and "basis" in original:
            eligible = _analysis_supported(original, applies, legacy_analysis)
        else:
            marked, sibling_basis = _marker(section, key)
            eligible = _basis(sibling_basis) in PROJECT_BASES if marked else whole
        if eligible:
            facts[key] = value
    return facts


def _number(value: Any) -> float | None:
    if not isinstance(value, str) and not is_number(value):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _axis(pose: dict[str, Any]) -> tuple[float, float, float] | None:
    q = pose.get("quaternion_wxyz")
    if not isinstance(q, list) or len(q) != 4 or not all(is_number(v) for v in q):
        return None
    w, x, y, z = q
    norm = w*w + x*x + y*y + z*z
    if not math.isclose(norm, 1, abs_tol=1e-9):
        return None
    return (2 * (x*z + w*y) / norm, 2 * (y*z - w*x) / norm, 1 - 2 * (x*x + y*y) / norm)


def _position(pose: Any) -> list[float] | None:
    value = _object(pose).get("position_mm")
    if isinstance(value, list) and len(value) == 3 and all(is_number(v) for v in value):
        return value
    return None


def _same_axis(a: tuple[float, float, float] | None, b: tuple[float, float, float] | None) -> bool:
    return a is not None and b is not None and all(abs(x - y) <= 1e-9 for x, y in zip(a, b))


def _dot(a: Any, b: Any) -> float:
    return sum(x * y for x, y in zip(a, b))


def _compose(parent: dict[str, Any], local: dict[str, Any]) -> dict[str, Any] | None:
    """The design-frame pose of `local` given in the frame of `parent`: position p + R(q)·p_local,
    orientation q ⊗ q_local (ObservationV1 with `device_instance_id`)."""
    p, pl = _position(parent), _position(local)
    q, ql = parent.get("quaternion_wxyz"), local.get("quaternion_wxyz")
    if p is None or pl is None or _axis(parent) is None or _axis(local) is None:
        return None
    w, x, y, z = q
    a, b, c, d = ql

    def cross(u: Any, v: Any) -> list[float]:
        return [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]]

    u = [x, y, z]
    t = [2 * value for value in cross(u, pl)]
    rotated = [v + w * tv + cv for v, tv, cv in zip(pl, t, cross(u, t))]
    return {
        "position_mm": [origin + offset for origin, offset in zip(p, rotated)],
        "quaternion_wxyz": [w * a - x * b - y * c - z * d, w * b + x * a + y * d - z * c,
                            w * c - x * d + y * a + z * b, w * d + x * c - y * b + z * a],
    }


def _observation_pose(project: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any] | None:
    """The observation's design-frame pose; an attached plane's pose is in its component's frame."""
    pose = _object(observation.get("pose"))
    device = observation.get("device_instance_id")
    if device is None:
        return pose
    hosts = [c for c in _objects(project.get("components")) if c.get("instance_id") == device]
    return _compose(_object(hosts[0].get("nominal_pose")), pose) if len(hosts) == 1 else None


def _light_enters_first_vertex(project: dict[str, Any], model: dict[str, Any], origin: list[float],
                               axis: tuple[float, float, float], first_vertex: float) -> bool:
    """Whether the configuration's light travels along the lens's +axis: every source lies before the
    first vertex on that axis and emits toward +axis, and the lens ports (when present) face +axis.
    Only then is an observation behind the last vertex on the image side."""
    for port in _objects(model.get("ports")):
        direction = port.get("direction")
        if port.get("role") in ("INPUT", "OUTPUT"):
            if not isinstance(direction, list) or len(direction) != 3:
                return False
            z = _number(direction[2])
            if z is None or z <= 0:
                return False
    sources = [c for c in _objects(project.get("components"))
               if c.get("optical_role") == "source" or "source_model" in c]
    if not sources:
        return False
    for source in sources:
        pose = _object(source.get("nominal_pose"))
        position, direction = _position(pose), _axis(pose)
        if position is None or direction is None or _dot(direction, axis) <= 0:
            return False
        if _dot(axis, [b - a for a, b in zip(origin, position)]) >= first_vertex:
            return False
    return True


def _models(project: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {m["model_id"]: _object(m.get("model")) for m in _objects(project.get("optical_models"))
            if isinstance(m.get("model_id"), str)}


def _native_geometry(project: dict[str, Any], view: dict[str, Any]) -> None:
    """Map centered sequential Projects with one lens component and one image observation."""
    models = _models(project)
    lenses = [c for c in _objects(project.get("components")) if c.get("optical_role") == "lens"]
    observations = _objects(project.get("observations"))
    if len(lenses) != 1:
        return
    lens = lenses[0]
    lens_id = lens.get("optical_model_id")
    if not isinstance(lens_id, str):
        return
    model = models.get(lens_id, {})
    members = _objects(model.get("members")) if model.get("model_kind") == "COMPOUND" else []
    placed = [(models.get(m["model_id"], {}), _object(m.get("pose"))) for m in members
              if isinstance(m.get("model_id"), str)]
    if not members:
        placed = [(model, {"position_mm": [0, 0, 0], "quaternion_wxyz": [1, 0, 0, 0]})]
    stops = [m for m, _ in placed if m.get("model_kind") == "APERTURE_STOP"]
    if len(stops) == 1:
        opening, edge = (_number(stops[0].get(k)) for k in ("opening_radius_mm", "edge_radius_mm"))
        if opening is not None and opening >= 0:
            view.setdefault("aperture", {})["stop_opening_diameter_mm"] = {
                "value": 2 * opening, "basis": "PROJECT_DECLARED", "system_aperture": True,
                "definition": "Diameter of the native aperture stop opening.",
            }
            if edge is not None:
                blocking = edge > opening
                view["design"]["aperture_validity"] = {
                    "state": "STOP_BLOCKING" if blocking else "STOP_NOT_BLOCKING",
                    "stop_blocks_light": blocking,
                    "eligible_for_stop_dependent_evaluation": blocking,
                }
    if len(observations) != 1:
        return
    observation = observations[0]
    pose, image_pose = _object(lens.get("nominal_pose")), _observation_pose(project, observation)
    if image_pose is None:
        return
    axis, origin, image = _axis(pose), _position(pose), _position(image_pose)
    if axis is None or origin is None or image is None or not _same_axis(axis, _axis(image_pose)):
        return
    offset = [b - a for a, b in zip(origin, image)]
    image_z = sum(a * b for a, b in zip(axis, offset))
    if any(abs(a - image_z * b) > 1e-8 for a, b in zip(offset, axis)):
        return
    vertices: list[tuple[float, bool]] = []
    plates: list[tuple[float, str, float]] = []
    for model, member_pose in placed:
        if model.get("model_kind") == "APERTURE_STOP":
            continue
        translation = _position(member_pose)
        surfaces = _objects(model.get("surfaces"))
        if (model.get("model_kind") != "SURFACE_SEQUENCE" or not surfaces or translation is None
                or translation[:2] != [0, 0] or _axis(member_pose) != (0, 0, 1)):
            return
        zs = [_number(s.get("vertex_z_mm")) for s in surfaces]
        if any(z is None for z in zs):
            return
        offset_z = translation[2]
        powered = [s.get("radius_mm") is not None or s.get("curvature_per_mm") is not None for s in surfaces]
        vertices.extend((offset_z + z, has_power) for z, has_power in zip(zs, powered))
        media = model.get("media")
        if len(zs) == 2 and not any(powered) and isinstance(media, list) and len(media) == 1 and isinstance(media[0], str):
            plates.append((offset_z + zs[0], media[0], zs[1] - zs[0]))
    if (not vertices or image_z < max(z for z, _ in vertices)
            or not _light_enters_first_vertex(project, model, origin, axis, min(z for z, _ in vertices))):
        # Not behind the last optical surface in the direction the configuration's light travels:
        # the observation is not an image receiver, so it supplies no image-format or back-focus fact.
        return
    receiver = _object(observation.get("aperture"))
    offset = receiver.get("center_offset_mm", [0, 0])
    centred = (isinstance(offset, list) and len(offset) == 2
               and all(_number(v) is not None and abs(_number(v)) <= 1e-9 for v in offset))
    if receiver.get("shape") == "rectangle" and centred:
        # width_mm and height_mm are the receiver's own u and v extents (its local x and y).
        width, height = (_number(receiver.get(k)) for k in ("half_width_mm", "half_height_mm"))
        if width is not None and height is not None and width > 0 and height > 0:
            view.setdefault("image_format", {}).update(width_mm=2 * width, height_mm=2 * height)
    order = view.setdefault("first_order", {})
    order["total_track_mm"] = {
        "value": image_z - min(z for z, _ in vertices), "basis": "PROJECT_DERIVED",
        "conditions": {"from": "first_optical_surface_vertex", "to": "fixed_image_plane"},
    }
    curved = [z for z, powered in vertices if powered]
    if curved:
        final = max(curved)
        trailing = [plate for plate in plates if plate[0] >= final]
        order["final_powered_vertex_to_sensor_mm"] = {
            "value": image_z - final, "basis": "PROJECT_DERIVED",
            "conditions": {"distance_kind": "geometric_axial_distance", "from": "last_powered_surface_vertex",
                           "to": "fixed_image_plane", "includes_cover_glass": bool(trailing)},
        }
        if len(trailing) == 1:
            _, material, thickness = trailing[0]
            view.setdefault("image_format", {})["cover_glass"] = {"material": material, "thickness_mm": thickness}


def _native_spectrum(project: dict[str, Any], primary: Any) -> dict[str, Any]:
    sources = [_object(c.get("source_model")) for c in _objects(project.get("components"))]
    lines = [[(s.get("wavelength_nm"), s.get("power_w")) for s in _objects(source.get("spectral_lines"))]
             for source in sources if source.get("spectral_lines")]
    single = [(s["wavelength_nm"], s.get("power_w")) for s in sources if is_number(s.get("wavelength_nm"))]
    samples = set(w for w, _ in single)
    samples.update(w for spectrum in lines for w, _ in spectrum if is_number(w))
    if not samples:
        return {}
    wavelengths = sorted(samples)
    start, end = wavelengths[0], wavelengths[-1]
    out = {
        "sampling_wavelengths_nm": wavelengths,
        "sampling_wavelengths_basis": "PROJECT_DECLARED",
        "sampling_span_nm": {"min_nm": start, "max_nm": end, "eligible_for": "DISCOVERY_ONLY"},
        "band_facets": [band for band, lo, hi in BANDS if start < hi and end >= lo],
    }
    if is_number(primary):
        out["primary_wavelength_nm"] = primary
        out["primary_wavelength_basis"] = "ENGINE_COMPUTED"
    weights = []
    if lines and not single and all(line == lines[0] for line in lines) and [w for w, _ in lines[0]] == wavelengths:
        weights = [power for _, power in lines[0]]
    if single and not lines and len(wavelengths) > 1 and all(is_number(p) and p > 0 for _, p in single):
        totals = [sum(p for nm, p in single if nm == wavelength) for wavelength in wavelengths]
        weights = totals
    if weights and all(is_number(p) and p > 0 for p in weights):
        maximum = max(weights)
        out["incident_weights"] = [p / maximum for p in weights]
        out["incident_weights_basis"] = "PROJECT_DERIVED"
    return out


def project_view(record: dict[str, Any], project: Any, *, as_written: bool,
                 applies: Callable[[dict[str, Any]], bool], camera_evidence: bool) -> dict[str, Any]:
    """Project the already-resolved record configuration onto the retrieval contract."""
    collection = _object(record.get("collection")).get("collection_id")
    collection = collection if isinstance(collection, str) else None
    view = _pick(record, "record_id", "record_revision", "configuration")
    view["collection"] = _pick(record.get("collection"), "collection_id")
    design = _object(record.get("design"))
    view["design"] = _pick(design, *DESIGN_CLASSIFICATION)
    view["design"]["configuration_count"] = design.get("configuration_count", 1)
    validity = _object(design.get("aperture_validity"))
    view["design"]["aperture_validity"] = _pick(
        validity, "state", "stop_blocks_light", "eligible_for_stop_dependent_evaluation",
    )
    legacy_camera = as_written and camera_evidence and collection in CAMERA_COLLECTIONS
    nested = ANALYSIS_BASES if legacy_camera else PROJECT_BASES
    for name in ("first_order", "aperture", "field", "image_format", "spectrum", "cost_proxy"):
        section = _object(record.get(name))
        if name == "cost_proxy" and legacy_camera:
            section = _pick(section, "basis", "scope", "weighted_spherical_element_count", "spherical_element_weight",
                            "aspheric_element_weight", "source")
        section = _fact_section(section, applies, legacy_analysis=legacy_camera and name == "cost_proxy",
                                admitted=nested)
        if section:
            view[name] = section
    formats = _pick(record.get("image_format"), "format_facets")
    if formats:
        view.setdefault("image_format", {}).update(formats)
    legacy_metrics = ("mtf", "mtf_field_minimum", "distortion_range")
    performance = [_clean(item, applies, nested) for item in _objects(record.get("performance"))
                   if _analysis_supported(item, applies, legacy_camera and item.get("metric") in legacy_metrics)]
    if performance:
        view["performance"] = performance
    studies = _object(record.get("analysis_evidence"))
    if studies:
        view["analysis_evidence"] = _clean(studies, applies, nested)

    check = _object(_object(record.get("first_order")).get("engine_check"))
    wavelength = check.get("wavelength_nm") if check.get("basis") == "ENGINE_COMPUTED" else None
    if as_written and collection in PARAXIAL_CHECK_COLLECTIONS and is_number(wavelength):
        for name, definition in (
            ("effective_focal_length_mm", "Paraxial effective focal length of the native Project."),
            ("back_focal_length_mm", "Distance from the last optical surface vertex to the paraxial rear focal point."),
        ):
            if is_number(check.get(name)):
                view.setdefault("first_order", {})[name] = {
                    "value": check[name], "basis": "ENGINE_COMPUTED", "definition": definition,
                    "conditions": {"wavelength_nm": wavelength},
                }
    if as_written:
        _native_geometry(_object(project), view)
        spectrum = _native_spectrum(_object(project), wavelength)
        if spectrum:
            projected_spectrum = view.setdefault("spectrum", {})
            projected_spectrum.update(spectrum)
            projected_spectrum.setdefault("continuous_coverage_nm", {"state": "UNCONFIRMED", "min_nm": None,
                                                                    "max_nm": None})
    if legacy_camera:
        key = "final_powered_vertex_to_sensor_mm"
        geometry = _object(view.get("first_order")).get(key)
        reference = _object(_object(record.get("first_order")).get(key)).get("source")
        if isinstance(geometry, dict) and isinstance(reference, dict) and applies(reference):
            geometry.update(_clean({"source": reference}, applies))
        for key in ("powered_element_count", "powered_group_count", "aspheric_element_count", "powered_element_material_class"):
            if key in design:
                view["design"][key] = design[key]
        field = _object(record.get("field"))
        view.setdefault("field", {}).update(_pick(field, "kind", "max_image_height_mm", "image_height_kind",
                                                   "half_field_deg", "field_points_mm"))
        image_height = field.get("max_image_height_mm")
        if is_number(image_height):
            view.setdefault("image_format", {})["image_circle_diameter_mm"] = 2 * image_height
        # This legacy SOURCE_STATED entry describes the admitted-pupil analysis of the nominal P2b
        # study preserved in the committed Project. The brief's sensor pixel pitch is independent.
        pupil = _object(_object(record.get("aperture")).get("entrance_pupil_f_number"))
        if (collection == "yutong-cup" and record.get("record_id") == "yutong-2025-mirrorless-18-p2b"
                and _object(pupil.get("conditions")).get("convention") == "efl_over_admitted_entrance_pupil_diameter"
                and is_number(pupil.get("value"))):
            fact = _pick(pupil, "value", "conditions", "definition", "system_aperture")
            view.setdefault("aperture", {})["entrance_pupil_f_number"] = dict(fact, basis="ENGINE_COMPUTED")
    return view

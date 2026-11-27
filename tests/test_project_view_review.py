"""Production CLI regressions for the acceptance follow-up of `0864a9b`.

1. An observation that is not behind the lens on its axis is not an image receiver, so it supplies
   no image-format, track or back-focus fact. "Behind" follows the configuration's light: a source
   on the far side, a reversed source or a flipped lens leaves the receiver unconfirmed, and a plane
   attached to a component (`device_instance_id`) is placed in that component's frame first.
2. A value paired with a `<name>_basis` marker that is not a Project basis never reaches the search
   projection or ordinary inspection, at any depth, including `_from_<word>` value names. A nested
   object or analysis study whose own basis is not a Project basis (or DERIVED) is excluded too.

Both reproductions mutate scratch copies of committed records and keep the committed corpus
unchanged.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from os_optics_lens.canonical import record_hash  # noqa: E402

LIBRARY = "lenslibrary-528155"
P2B = "yutong-2025-mirrorless-18-p2b"
LIBRARY_DIR = Path("records/lenslibrary") / LIBRARY
P2B_DIR = Path("records/yutong-cup") / P2B
ALL = ["MATCH", "UNCONFIRMED", "DOES_NOT_MATCH"]
SENSOR = {"id": "sensor", "scope": ["image_format"],
          "predicates": [{"path": "width_mm", "op": "eq", "value": 36},
                         {"path": "height_mm", "op": "eq", "value": 24}]}
MONITOR_APERTURE = {"shape": "rectangle", "half_width_mm": 18, "half_height_mm": 12, "center_offset_mm": [0, 0]}
TRACK = {"id": "track", "fact": "first_order.total_track_mm", "op": "gt", "value": 0}
BFD = {"id": "bfd", "fact": "first_order.final_powered_vertex_to_sensor_mm", "op": "gt", "value": 0}
LENS_Q = [0.5, 0.5, 0.5, 0.5]
"""The committed lens and image-plane orientation: local +Z along design +x."""
FLIPPED_Q = [-0.5, 0.5, 0.5, -0.5]
"""`[0, 0, 1, 0] * LENS_Q`: local +Z along design -x."""
RECEIVER_KEYS = ("image_format.width_mm", "image_format.height_mm", "first_order.total_track_mm",
                 "first_order.final_powered_vertex_to_sensor_mm")


def write_json(path: Path, data: Any) -> bytes:
    payload = (json.dumps(data, indent=1, ensure_ascii=False) + "\n").encode("utf-8")
    path.write_bytes(payload)
    return payload


def read_json(path: Path) -> Any:
    return json.loads(path.read_bytes().replace(b"\r\n", b"\n"))


class ScratchCorpus:
    """A copy of the committed corpus whose records can be mutated and rehashed."""

    def __init__(self, parent: Path, name: str) -> None:
        self.root = parent / name
        for part in ("records", "licenses", "acceptance"):
            shutil.copytree(ROOT / part, self.root / part)
        shutil.copy(ROOT / "LICENSE", self.root / "LICENSE")
        self.index = parent / f"{name}.sqlite"

    def edit_record(self, record_dir: Path, mutate) -> None:
        path = self.root / record_dir / "record.json"
        record = read_json(path)
        mutate(record)
        record["record_hash"] = record_hash(record)
        write_json(path, record)

    def edit_project(self, record_dir: Path, mutate) -> None:
        path = self.root / record_dir / "project.json"
        project = read_json(path)
        mutate(project)
        digest = hashlib.sha256(write_json(path, project)).hexdigest()
        self.edit_record(record_dir, lambda record: record["native_project"].__setitem__("sha256", digest))


def cli(index: Path | None, *args: str, request: Any = None) -> tuple[int, dict[str, Any]]:
    command = [sys.executable, "-m", "os_optics_lens"]
    if index is not None:
        command += ["--index", str(index)]
    proc = subprocess.run(command + list(args), input=json.dumps(request).encode() if request is not None else b"",
                          env=dict(os.environ, PYTHONPATH=str(SRC)), capture_output=True, timeout=120)
    return proc.returncode, json.loads(proc.stdout)


class CliCase(unittest.TestCase):
    def ok(self, index: Path | None, *args: str, request: Any = None) -> dict[str, Any]:
        code, doc = cli(index, *args, request=request)
        self.assertEqual(code, 0, doc)
        self.assertTrue(doc["ok"], doc)
        return doc["result"]

    def build(self, corpus: ScratchCorpus) -> Path:
        self.ok(None, "index", "build", "--corpus", str(corpus.root), "--out", str(corpus.index))
        return corpus.index

    def evaluate(self, index: Path, record_id: str, *constraints: dict[str, Any]) -> dict[str, Any]:
        request = {"mode": "specification", "filters": {"record_ids": [record_id]}, "include": ALL,
                   "detail": "full", "constraints": list(constraints)}
        [candidate] = self.ok(index, "search", "-", request=request)["candidates"]
        return candidate["constraints"]


class ImageReceiverRole(CliCase):
    @classmethod
    def setUpClass(cls) -> None:
        work = tempfile.TemporaryDirectory(prefix="os-optics-lens-receiver-")
        cls.addClassCleanup(work.cleanup)
        cls.work = Path(work.name)

    def monitor_corpus(self, name: str, pose: dict[str, Any], *, device: str | None = None,
                       components: dict[str, dict[str, Any]] | None = None) -> Path:
        """Replace the observation with one rectangle plane; `components` overrides the nominal
        pose of the named component instances."""
        corpus = ScratchCorpus(self.work, name)

        def replace(project: dict[str, Any]) -> None:
            observation = {"observation_id": "entrance_monitor", "absorbing": False,
                           "pose": pose, "aperture": copy.deepcopy(MONITOR_APERTURE)}
            if device is not None:
                observation["device_instance_id"] = device
            project["observations"] = [observation]
            for component in project["components"]:
                if component["instance_id"] in (components or {}):
                    component["nominal_pose"] = components[component["instance_id"]]

        corpus.edit_project(LIBRARY_DIR, replace)
        return self.build(corpus)

    def assert_no_receiver(self, index: Path) -> None:
        result = self.evaluate(index, LIBRARY, SENSOR, TRACK, BFD)
        for name in ("sensor", "track", "bfd"):
            self.assertEqual((result[name]["outcome"], result[name].get("reason")), ("UNCONFIRMED", "FACT_MISSING"), name)
        inspected = self.ok(index, "inspect", LIBRARY)
        for key in RECEIVER_KEYS:
            self.assertNotIn(key, inspected["fact_keys"])

    def image_pose(self) -> dict[str, Any]:
        return copy.deepcopy(read_json(ROOT / LIBRARY_DIR / "project.json")["observations"][0]["pose"])

    def test_object_side_monitor_supplies_no_image_format(self) -> None:
        pose = self.image_pose()
        pose["position_mm"] = [-25, 0, 0]
        index = self.monitor_corpus("object-side", pose)
        track = {"id": "track", "fact": "first_order.total_track_mm", "op": "gt", "value": 0}
        result = self.evaluate(index, LIBRARY, SENSOR, track)
        for name in ("sensor", "track"):
            self.assertEqual((result[name]["outcome"], result[name].get("reason")), ("UNCONFIRMED", "FACT_MISSING"), name)
        inspected = self.ok(index, "inspect", LIBRARY)
        for key in ("image_format.width_mm", "image_format.height_mm", "first_order.total_track_mm",
                    "first_order.final_powered_vertex_to_sensor_mm"):
            self.assertNotIn(key, inspected["fact_keys"])
        facts = self.ok(index, "fields", "--filter", f"family={LIBRARY}", "--prefix", "image_format")
        self.assertNotIn("image_format.width_mm", facts["fields"])

    def test_same_monitor_behind_the_lens_is_the_receiver(self) -> None:
        index = self.monitor_corpus("image-side", self.image_pose())
        result = self.evaluate(index, LIBRARY, SENSOR)
        self.assertEqual(result["sensor"]["outcome"], "MATCH")

    def test_monitor_before_a_reversed_source_is_not_the_receiver(self) -> None:
        # The source sits beyond the lens on its +axis and emits toward -axis, so a plane between
        # the source and the lens is on the object side even though it is behind the last vertex.
        reversed_source = {"position_mm": [600.0, 0.0, 0.0], "quaternion_wxyz": FLIPPED_Q}
        for name, x in (("reversed-source", 261.417804), ("reversed-source-400", 400.0)):
            pose = {"position_mm": [x, 0.0, 0.0], "quaternion_wxyz": LENS_Q}
            self.assert_no_receiver(self.monitor_corpus(name, pose, components={"SRC": reversed_source}))

    def test_monitor_before_a_flipped_lens_is_not_the_receiver(self) -> None:
        # The lens faces -x; the source at -100 still emits toward +x, so the plane at -60 is hit
        # before the lens although it lies behind the last vertex on the lens's local +Z.
        pose = {"position_mm": [-60.0, 0.0, 0.0], "quaternion_wxyz": FLIPPED_Q}
        lens = {"position_mm": [0.0, 0.0, 0.0], "quaternion_wxyz": FLIPPED_Q}
        self.assert_no_receiver(self.monitor_corpus("reversed-lens", pose, components={"LENS": lens}))

    def test_attached_plane_is_placed_in_its_component_frame(self) -> None:
        # Attached to LENS or SRC, local pose [261.4, 0, 0] is design pose [0, 261.4, 0] (or
        # [-100, 261.4, 0]) facing +y: off the lens axis, so not the image receiver.
        local = {"position_mm": [261.417804, 0.0, 0.0], "quaternion_wxyz": LENS_Q}
        for device in ("LENS", "SRC"):
            self.assert_no_receiver(self.monitor_corpus(f"attached-{device}", local, device=device))
        # The converse: the true image plane expressed in the lens frame is the receiver.
        lens_local = {"position_mm": [0.0, 0.0, 261.417804], "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0]}
        result = self.evaluate(self.monitor_corpus("attached-lens-local", lens_local, device="LENS"),
                               LIBRARY, SENSOR, TRACK, BFD)
        self.assertEqual({name: entry["outcome"] for name, entry in result.items()},
                         {"sensor": "MATCH", "track": "MATCH", "bfd": "MATCH"})
        self.assertAlmostEqual(result["track"]["fact"]["value"], 261.417804)

    def test_p2b_receiver_is_unchanged(self) -> None:
        corpus = ScratchCorpus(self.work, "p2b")
        result = self.evaluate(self.build(corpus), P2B, SENSOR)
        self.assertEqual(result["sensor"]["outcome"], "MATCH")


class SourceMarkedValues(CliCase):
    """`<name>_basis` markers pair with `name` or its unit stem; a pair whose marker is not a
    Project basis leaves the projection together, wherever it is nested."""

    @classmethod
    def setUpClass(cls) -> None:
        work = tempfile.TemporaryDirectory(prefix="os-optics-lens-markers-")
        cls.addClassCleanup(work.cleanup)
        corpus = ScratchCorpus(Path(work.name), "markers")

        def mark(record: dict[str, Any]) -> None:
            entry = next(item for item in record["performance"] if item["metric"] == "mtf_field_minimum")
            entry["reference_mtf"] = 0.9
            entry["reference_mtf_basis"] = "SOURCE_STATED"
            entry["reference_contrast_percent"] = 90
            entry["reference_contrast_basis"] = "SOURCE_STATED in the publication"
            entry["review_mtf"] = 0.95
            entry["review_mtf_basis"] = "contributor estimate"
            entry["model_mtf"] = 0.72
            entry["model_mtf_basis"] = "ENGINE_COMPUTED"
            # `_from_<word>` value names pair with the marker of their stem.
            entry["vendor_mtf_from_datasheet"] = 0.91
            entry["vendor_mtf_basis"] = "SOURCE_STATED"
            # Nested objects whose own basis is not a Project basis, in any spelling.
            entry["lower"] = {"value": 0.93, "basis": "source_stated"}
            entry["punct"] = {"value": 0.94, "basis": "SOURCE_STATED; brief"}
            entry["vendor"] = {"value": 0.95, "basis": "VENDOR_STATED"}
            entry["derived"] = {"value": 0.96, "basis": "DERIVED"}
            efl = record["first_order"]["effective_focal_length_mm"]
            efl["catalog_mm_from_datasheet"] = 18
            efl["catalog_basis"] = "SOURCE_STATED"
            tolerance = record["analysis_evidence"]["tolerance"]
            tolerance["catalog_yield_fraction_from_vendor"] = 0.97
            tolerance["catalog_yield_fraction_basis"] = "SOURCE_STATED"
            gate = tolerance["mtf_gate"]
            gate["reference_passed"] = 96
            gate["reference_passed_basis"] = "SOURCE_STATED"
            record["analysis_evidence"]["vendor_yield"] = {"status": "measured", "basis": "VENDOR_STATED",
                                                           "yield_fraction": 0.99}

        def price(record: dict[str, Any]) -> None:
            record["cost_proxy"] = {"basis": "PROJECT_DERIVED", "weighted_spherical_element_count": 4,
                                    "spherical_element_weight": 1, "aspheric_element_weight": 3,
                                    "vendor_price_usd_from_catalog": 120, "vendor_price_usd_basis": "SOURCE_STATED"}

        corpus.edit_record(P2B_DIR, mark)
        corpus.edit_record(LIBRARY_DIR, price)
        cls.index = SourceMarkedValues.build(cls(), corpus)

    def full_form(self, cid: str, path: str, op: str, value: Any) -> dict[str, Any]:
        return {"id": cid, "scope": ["performance"], "metric": "mtf_field_minimum",
                "predicates": [{"path": path, "op": op, "value": value}]}

    def test_source_marked_values_cannot_match_in_either_threshold_direction(self) -> None:
        constraints = [
            self.full_form("high", "reference_mtf", "gte", 0.85),
            self.full_form("low", "reference_mtf", "lte", 0.95),
            self.full_form("percent", "reference_contrast_percent", "gte", 80),
            self.full_form("unclassified", "review_mtf", "gte", 0.9),
            {"id": "shorthand", "fact": "performance.mtf_field_minimum", "path": "reference_mtf", "op": "gte",
             "value": 0.85},
            {"id": "gate", "scope": ["analysis_evidence", "tolerance", "mtf_gate"],
             "predicates": [{"path": "reference_passed", "op": "gte", "value": 90}]},
        ]
        result = self.evaluate(self.index, P2B, *constraints)
        for constraint in constraints:
            entry = result[constraint["id"]]
            self.assertEqual((entry["outcome"], entry["reason"]), ("UNCONFIRMED", "VALUE_MISSING"), constraint["id"])

    def test_from_suffixed_and_nested_source_values_cannot_match(self) -> None:
        constraints = [
            self.full_form("from", "vendor_mtf_from_datasheet", "gte", 0.85),
            self.full_form("lower", "lower.value", "gte", 0.85),
            self.full_form("punct", "punct.value", "gte", 0.85),
            self.full_form("vendor", "vendor.value", "gte", 0.85),
            {"id": "efl", "scope": ["first_order", "effective_focal_length_mm"],
             "predicates": [{"path": "catalog_mm_from_datasheet", "op": "eq", "value": 18}]},
            {"id": "yield", "scope": ["analysis_evidence", "tolerance"],
             "predicates": [{"path": "catalog_yield_fraction_from_vendor", "op": "gte", "value": 0.9}]},
            {"id": "study", "scope": ["analysis_evidence", "vendor_yield"],
             "predicates": [{"path": "yield_fraction", "op": "gte", "value": 0.9}]},
        ]
        result = self.evaluate(self.index, P2B, *constraints)
        for constraint in constraints:
            self.assertEqual(result[constraint["id"]]["outcome"], "UNCONFIRMED", constraint["id"])
        # A nested object with an admitted basis stays.
        self.assertEqual(self.evaluate(self.index, P2B, self.full_form("derived", "derived.value", "gte", 0.9))
                         ["derived"]["outcome"], "MATCH")
        price = {"id": "price", "fact": "cost_proxy.vendor_price_usd_from_catalog", "op": "lte", "value": 200}
        self.assertEqual(self.evaluate(self.index, LIBRARY, price)["price"]["outcome"], "UNCONFIRMED")
        listed = self.ok(self.index, "fields", "--prefix", "cost_proxy.vendor")
        self.assertNotIn("cost_proxy.vendor_price_usd_from_catalog", listed["fields"])
        inspected = json.dumps(self.ok(self.index, "inspect", LIBRARY, "--sections", "cost_proxy"))
        self.assertNotIn("vendor_price", inspected)

    def test_project_values_stay_available(self) -> None:
        result = self.evaluate(self.index, P2B, self.full_form("minimum", "value", "gt", 0.7),
                               self.full_form("model", "model_mtf", "gt", 0.7))
        self.assertEqual(result["minimum"]["outcome"], "MATCH")
        self.assertAlmostEqual(result["minimum"]["fact"]["value"], 0.7228471580223309)
        self.assertEqual(result["model"]["outcome"], "MATCH")
        # `field_basis` is a field-coordinate convention, not a basis marker: it qualifies no value.
        pinned = dict(self.full_form("pinned", "value", "gt", 0.7),
                      conditions={"field_basis": "diagonal_image_height_fraction", "frequency_lp_mm": 40})
        self.assertEqual(self.evaluate(self.index, P2B, pinned)["pinned"]["outcome"], "MATCH")

    def test_ordinary_inspection_omits_source_marked_pairs(self) -> None:
        inspected = self.ok(self.index, "inspect", P2B, "--fields", "performance.mtf_field_minimum",
                            "--sections", "performance,analysis_evidence")
        text = json.dumps(inspected)
        for name in ("reference_mtf", "reference_contrast", "review_mtf", "reference_passed", "vendor_mtf",
                     "catalog_yield", "vendor_yield", "source_stated", "VENDOR_STATED", "0.93", "0.94"):
            self.assertNotIn(name, text)
        self.assertIn('"model_mtf": 0.72', text)
        self.assertIn('"model_mtf_basis": "ENGINE_COMPUTED"', text)


class ProjectionEdges(CliCase):
    """Second review pass: off-axis active areas, marker gating, nested bases and coverage."""

    @classmethod
    def setUpClass(cls) -> None:
        work = tempfile.TemporaryDirectory(prefix="os-optics-lens-edges-")
        cls.addClassCleanup(work.cleanup)
        cls.work = Path(work.name)

    def test_off_axis_active_area_supplies_no_width_or_height(self) -> None:
        corpus = ScratchCorpus(self.work, "offset")

        def offset(project: dict[str, Any]) -> None:
            observation = project["observations"][0]
            observation["aperture"] = dict(MONITOR_APERTURE, center_offset_mm=[30, 0])

        corpus.edit_project(LIBRARY_DIR, offset)
        index = self.build(corpus)
        result = self.evaluate(index, LIBRARY, SENSOR, TRACK)
        self.assertEqual((result["sensor"]["outcome"], result["sensor"]["reason"]), ("UNCONFIRMED", "FACT_MISSING"))
        self.assertEqual(result["track"]["outcome"], "MATCH")

    def test_marker_gating_nested_bases_and_coverage(self) -> None:
        corpus = ScratchCorpus(self.work, "edges")

        def p2b(record: dict[str, Any]) -> None:
            entry = next(item for item in record["performance"] if item["metric"] == "mtf_field_minimum")
            # A field-coordinate convention beside a real Project field angle keeps both.
            entry["conditions"]["field_deg"] = 49.5
            # Basis-class markers pair with any `name_...` member and ignore case.
            entry["quoted_mtf_ratio"] = 0.91
            entry["quoted_mtf_basis"] = "SOURCE_STATED"
            entry["Upper_MTF"] = 0.92
            entry["upper_mtf_basis"] = "VENDOR_STATED"
            # A legacy camera analysis keeps a DERIVED pair.
            entry["derived_mtf"] = 0.72
            entry["derived_mtf_basis"] = "DERIVED"
            record["spectrum"]["continuous_coverage_nm"] = {"state": "SOURCE_STATED", "min_nm": 400, "max_nm": 700,
                                                            "basis": "ENGINE_COMPUTED"}

        def library(record: dict[str, Any]) -> None:
            # Outside the legacy camera analyses a nested DERIVED value is not a Project fact.
            record["performance"] = [{"metric": "mtf_field_minimum", "basis": "ENGINE_COMPUTED", "value": 0.5,
                                      "derived_mtf": 0.9, "derived_mtf_basis": "DERIVED from the brief"}]

        corpus.edit_record(P2B_DIR, p2b)
        corpus.edit_record(LIBRARY_DIR, library)
        index = self.build(corpus)

        def entry_path(cid: str, path: str, op: str, value: Any) -> dict[str, Any]:
            return {"id": cid, "scope": ["performance"], "metric": "mtf_field_minimum",
                    "predicates": [{"path": path, "op": op, "value": value}]}

        result = self.evaluate(
            index, P2B,
            dict(entry_path("convention", "value", "gt", 0.7),
                 conditions={"field_basis": "diagonal_image_height_fraction", "field_deg": 49.5}),
            entry_path("quoted", "quoted_mtf_ratio", "gte", 0.9),
            entry_path("upper", "Upper_MTF", "gte", 0.9),
            entry_path("derived", "derived_mtf", "gt", 0.7),
            {"id": "coverage", "fact": "spectrum.continuous_coverage_nm", "op": "covers_interval",
             "value": [450, 650]},
        )
        self.assertEqual(result["convention"]["outcome"], "MATCH")
        for name in ("quoted", "upper"):
            self.assertEqual((result[name]["outcome"], result[name]["reason"]), ("UNCONFIRMED", "VALUE_MISSING"), name)
        self.assertEqual(result["derived"]["outcome"], "MATCH")
        self.assertEqual(result["coverage"]["outcome"], "UNCONFIRMED")

        result = self.evaluate(index, LIBRARY, entry_path("nested", "derived_mtf", "gte", 0.85))
        self.assertEqual((result["nested"]["outcome"], result["nested"]["reason"]), ("UNCONFIRMED", "VALUE_MISSING"))


if __name__ == "__main__":
    unittest.main()

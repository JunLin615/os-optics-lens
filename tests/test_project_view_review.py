"""Production CLI regressions for the acceptance follow-up of `0864a9b`.

1. An observation that is not behind the lens on its axis is not an image receiver, so it supplies
   no image-format, track or back-focus fact.
2. A value paired with a `<name>_basis` marker that is not a Project basis never reaches the search
   projection or ordinary inspection, at any depth.

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

    def monitor_corpus(self, name: str, pose: dict[str, Any]) -> Path:
        corpus = ScratchCorpus(self.work, name)

        def replace(project: dict[str, Any]) -> None:
            project["observations"] = [{"observation_id": "entrance_monitor", "absorbing": False,
                                        "pose": pose, "aperture": copy.deepcopy(MONITOR_APERTURE)}]

        corpus.edit_project(LIBRARY_DIR, replace)
        return self.build(corpus)

    def image_pose(self) -> dict[str, Any]:
        return copy.deepcopy(read_json(ROOT / LIBRARY_DIR / "project.json")["observations"][0]["pose"])

    def test_object_side_monitor_supplies_no_image_format(self) -> None:
        pose = self.image_pose()
        pose["position_mm"] = [-25, 0, 0]
        index = self.monitor_corpus("object-side", pose)
        track = {"id": "track", "fact": "first_order.total_track_mm", "op": "gt", "value": 0}
        result = self.evaluate(index, LIBRARY, SENSOR, track)
        for name in ("sensor", "track"):
            self.assertEqual((result[name]["outcome"], result[name]["reason"]), ("UNCONFIRMED", "FACT_MISSING"), name)
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
            gate = record["analysis_evidence"]["tolerance"]["mtf_gate"]
            gate["reference_passed"] = 96
            gate["reference_passed_basis"] = "SOURCE_STATED"

        corpus.edit_record(P2B_DIR, mark)
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
        for name in ("reference_mtf", "reference_contrast", "review_mtf", "reference_passed"):
            self.assertNotIn(name, text)
        self.assertIn('"model_mtf": 0.72', text)
        self.assertIn('"model_mtf_basis": "ENGINE_COMPUTED"', text)


if __name__ == "__main__":
    unittest.main()

"""Exercise opt-in provenance and supporting calculations on the committed Projects."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from os_optics_lens.build import build_index  # noqa: E402

LIBRARY = "lenslibrary-528155"
P2B = "yutong-2025-mirrorless-18-p2b"
EFL = "first_order.effective_focal_length_mm"
BFL = "first_order.back_focal_length_mm"
ILLUMINATION = "performance.relative_illumination"


class ProjectProvenanceCli(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        work = tempfile.TemporaryDirectory(prefix="os-optics-lens-provenance-")
        cls.addClassCleanup(work.cleanup)
        cls.index = Path(work.name) / "production.sqlite"
        build_index(ROOT, cls.index)

    def cli(self, *args: str, request: dict | None = None) -> dict:
        proc = subprocess.run(
            [sys.executable, "-m", "os_optics_lens", "--index", str(self.index), *args],
            input=json.dumps(request).encode() if request is not None else b"",
            env=dict(os.environ, PYTHONPATH=str(SRC)), cwd=self.index.parent,
            capture_output=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)["result"]

    def test_direct_source_and_terms_require_explicit_inspection(self) -> None:
        library = self.cli("inspect", LIBRARY)
        p2b = self.cli("inspect", P2B)
        self.assertEqual(library["collection"], {"collection_id": "lenslibrary"})
        for ordinary in (library, p2b):
            for key in ("license", "provenance", "validation", "evidence_sections"):
                self.assertNotIn(key, ordinary)
            paths = {ref["path"] for ref in ordinary["evidence"]["refs"]}
            self.assertNotIn("LICENSE", paths)
            self.assertNotIn("licenses/LensLibrary-MIT.txt", paths)

        source = self.cli("inspect", LIBRARY, "--provenance")["provenance"]
        self.assertEqual(source["source"], "Nathan Hagen's LensLibrary")
        self.assertEqual(source["source_input"], "zemax_files/528155.zmx")
        self.assertRegex(source["source_revision"], r"^[0-9a-f]{40}$")
        self.assertIn(source["source_revision"], source["source_links"]["source_file_url"])
        self.assertIn("Nathan Hagen", source["attribution"])
        self.assertIn("MIT", source["license"]["redistribution"])
        self.assertEqual(source["license"]["notice_path"], "licenses/LensLibrary-MIT.txt")
        self.assertNotIn("source_prescription_dump", source)

        study = self.cli("inspect", P2B, "--provenance")["provenance"]
        self.assertIn("P2b_native/camera.json", study["source_input"])
        self.assertEqual(study["original_publication"]["id"], "WO2020213337A1")
        self.assertIn("numerical example 2", study["original_publication"]["reference"])
        self.assertEqual(study["license"]["spdx"], "MIT")
        self.assertEqual(study["license"]["notice_path"], "LICENSE")
        self.assertNotIn("evidence_archive", study)

    def test_inspect_and_handoff_keep_project_facts_and_configurations(self) -> None:
        inspected = self.cli("inspect", LIBRARY, "--sections", "first_order,spectrum", "--fields", EFL)
        self.assertEqual(inspected["fields"][EFL]["basis_class"], "ENGINE_COMPUTED")
        self.assertNotIn("checks", inspected["fields"][EFL])
        self.assertNotIn("engine_check", inspected["sections"]["first_order"])
        self.assertNotIn("sampling_wavelengths_air_nm", inspected["sections"]["spectrum"])

        project = self.cli("project", LIBRARY)
        self.assertEqual(project["configurations"], inspected["configurations"])
        self.assertEqual(project["sha256"], inspected["native_project"]["sha256"])
        self.assertEqual(project["revision"], inspected["revision"])
        for key in ("license", "attribution", "provenance"):
            self.assertNotIn(key, project)

    def test_paraxial_summaries_include_the_project_wavelength(self) -> None:
        request = {"mode": "starting_point", "filters": {"record_ids": [LIBRARY]}, "fields": [EFL, BFL]}
        [candidate] = self.cli("search", "-", request=request)["candidates"]
        compared = self.cli("compare", LIBRARY, "lenslibrary-1792917", "--fields", f"{EFL},{BFL}")
        for row in compared["rows"]:
            fact = candidate["facts"][row["fact"]]
            wavelength = fact["conditions"]["wavelength_nm"]
            self.assertGreater(wavelength, 0)
            self.assertEqual(row["values"][0]["conditions"]["wavelength_nm"], wavelength)

    def test_calculation_reference_availability_and_explicit_selection(self) -> None:
        inspected = self.cli("inspect", P2B, "--fields", ILLUMINATION, "--sections", "analysis_evidence",
                             "--evidence", "back_focus")
        self.assertNotIn("source", inspected["fields"][ILLUMINATION])
        self.assertNotIn("source", inspected["fields"][ILLUMINATION]["value"])
        self.assertNotIn("source", inspected["sections"]["analysis_evidence"]["tolerance"]["mtf_gate"])
        [document] = inspected["evidence_sections"].values()
        self.assertIn("value_mm", document["sections"]["back_focus"])
        self.assertIn("RESOLVED", {ref["status"] for ref in inspected["evidence"]["refs"]})

    def test_search_checks_source_explicitly_without_matching_reference_contents(self) -> None:
        request = {
            "mode": "specification", "filters": {"record_ids": [P2B]}, "detail": "full",
            "include": ["MATCH", "UNCONFIRMED", "DOES_NOT_MATCH"],
            "constraints": [
                {"id": "fact", "fact": ILLUMINATION, "op": "gt", "value": 0},
                {"id": "support", "fact": ILLUMINATION, "op": "has_source"},
                {"id": "nested_support", "scope": ["analysis_evidence", "tolerance"],
                 "predicates": [{"path": "mtf_gate.source", "op": "has_source", "value": True}]},
                {"id": "reference_member", "fact": ILLUMINATION, "path": "source.section",
                 "op": "ne", "value": "arbitrary"},
                {"id": "reference_selector", "fact": ILLUMINATION, "op": "gt", "value": 0,
                 "select": {"source": {}}},
            ],
        }
        [candidate] = self.cli("search", "-", request=request)["candidates"]
        constraints = candidate["constraints"]
        self.assertEqual(constraints["fact"]["outcome"], "MATCH")
        self.assertNotIn("source", constraints["fact"]["fact"])
        self.assertEqual(constraints["support"]["fact"]["source"]["status"], "RESOLVED")
        self.assertEqual(constraints["nested_support"]["outcome"], "MATCH")
        for key in ("reference_member", "reference_selector"):
            self.assertEqual(constraints[key]["outcome"], "UNCONFIRMED")
            self.assertEqual(constraints[key]["reason"], "COMPARISON_ONLY")

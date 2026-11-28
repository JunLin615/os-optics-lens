"""Check the telescope and observation batch against the record contract.

The checks fall into three groups:

* the batch manifest and the record directories agree, one outcome per candidate;
* every record is internally consistent: hashes, value bases, spectrum, field, aperture, the four
  validation outcomes and the arithmetic behind the paraxial comparison;
* the committed Project matches what the record says about it: one clear diameter per element, a
  closed body with positive edge thickness, and — where the engine is installed — the same opening,
  solid and paraxial results.

The engine checks are skipped when no ``os-optics`` executable is available, so the file runs in a
bare checkout and verifies the optics where the engine is present.
"""

import hashlib
import json
import math
import os
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COLLECTION = ROOT / "records/telescope-observation"
MANIFEST = json.loads((COLLECTION / "batch-telescope-observation-1.json").read_text(encoding="utf-8"))
RECORD_DIRS = sorted(p for p in COLLECTION.iterdir() if (p / "record.json").is_file())
RECORDS = {p.name: json.loads((p / "record.json").read_text(encoding="utf-8")) for p in RECORD_DIRS}

BASES = {"SOURCE_STATED", "DERIVED", "ENGINE_COMPUTED", "PROJECT_DERIVED"}
OUTCOMES = {"ACCEPTED", "DEFERRED", "CONVERSION_FAILED"}
PARAXIAL_STATES = {"MATCHED", "DIFFERS", "NOT_CHECKED"}
APERTURE_STATES = {"STOP_BLOCKING", "STOP_NOT_BLOCKING", "NO_STOP"}
SEVERITIES = {"ERROR", "WARNING", "INFO"}
BAND_FACETS = {
    "extreme_ultraviolet": (0.1, 10.0),
    "ultraviolet": (10.0, 380.0),
    "visible": (380.0, 780.0),
    "near_infrared": (780.0, 3000.0),
    "mid_wave_infrared": (3000.0, 8000.0),
    "long_wave_infrared": (8000.0, 15000.0),
}
# the seed batch's comparison rule, restated here so a record cannot widen it silently
ATOL_MM = 0.005
RTOL = 0.003


def sha256(path):
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def record_hash(body):
    payload = {k: v for k, v in body.items() if k not in ("record_hash", "record_hash_method")}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def engine_binary():
    candidate = os.environ.get("OS_OPTICS_BIN")
    if candidate and Path(candidate).is_file():
        return candidate
    for name in ("os-optics",):
        found = shutil.which(name)
        if found:
            return found
    for path in ("/opt/venv-os/bin/os-optics",):
        if Path(path).is_file():
            return path
    return None


ENGINE = engine_binary()


def run_engine(*args):
    result = subprocess.run([ENGINE, *args], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


class ManifestTest(unittest.TestCase):
    def test_one_row_per_candidate_and_counts_agree(self):
        rows = MANIFEST["outcomes"]
        self.assertEqual(MANIFEST["input_inventory"]["count"], len(rows))
        counts = MANIFEST["counts"]
        self.assertEqual(counts["inputs"], len(rows))
        self.assertEqual(counts["accepted"], sum(1 for r in rows if r["outcome"] == "ACCEPTED"))
        self.assertEqual(counts["deferred"], sum(1 for r in rows if r["outcome"] == "DEFERRED"))
        self.assertEqual(counts["conversion_failed"],
                         sum(1 for r in rows if r["outcome"] == "CONVERSION_FAILED"))
        self.assertEqual(counts["accepted"], len(MANIFEST["accepted_record_ids"]))

    def test_manifest_and_record_directories_are_the_same_set(self):
        self.assertEqual(sorted(MANIFEST["accepted_record_ids"]), [p.name for p in RECORD_DIRS])

    def test_every_row_has_one_outcome_and_a_reason_when_not_accepted(self):
        for row in MANIFEST["outcomes"]:
            self.assertIn(row["outcome"], OUTCOMES, row["source_input"])
            self.assertTrue(row["source_input"])
            if row["outcome"] == "ACCEPTED":
                self.assertIn(row["record_id"], MANIFEST["accepted_record_ids"])
                self.assertEqual(row["record_revision"], 1)
                self.assertRegex(row["source_input_sha256"] or "", r"^[0-9a-f]{64}$")
            else:
                self.assertIsNone(row["record_id"])
                self.assertTrue(row["reason_code"], row["source_input"])
                self.assertTrue(row["reason"], row["source_input"])

    def test_accepted_row_source_input_matches_its_record(self):
        for row in MANIFEST["outcomes"]:
            if row["outcome"] != "ACCEPTED":
                continue
            record = RECORDS[row["record_id"]]
            self.assertEqual(row["source_input"], record["collection"]["source_input"])
            self.assertEqual(row["record_revision"], record["record_revision"])


class RecordIntegrityTest(unittest.TestCase):
    def test_record_hash_recomputes(self):
        for name, record in RECORDS.items():
            self.assertEqual(record["record_hash"], record_hash(record), name)

    def test_committed_files_match_their_recorded_hashes(self):
        for name, record in RECORDS.items():
            project = ROOT / record["native_project"]["path"]
            self.assertEqual(project, RECORD_DIRS[[p.name for p in RECORD_DIRS].index(name)] / "project.json")
            self.assertEqual(sha256(project), record["native_project"]["sha256"], name)
            evidence = ROOT / record["collection"]["evidence_extract"]["path"]
            self.assertEqual(sha256(evidence), record["collection"]["evidence_extract"]["sha256"], name)
            # the transcription is the input: the record's source input hash is the evidence's
            self.assertEqual(record["collection"]["source_input_sha256"],
                             record["collection"]["evidence_extract"]["sha256"], name)

    def test_identity_and_provenance_are_complete(self):
        for name, record in RECORDS.items():
            self.assertEqual(record["schema"], "opensci.lens.record")
            self.assertEqual(record["record_schema_version"], "1.0")
            self.assertEqual(record["record_id"], name)
            self.assertIsInstance(record["record_revision"], int)
            self.assertTrue(record["title"])
            collection = record["collection"]
            self.assertEqual(collection["collection_id"], "telescope-observation")
            self.assertTrue(collection["attribution"])
            self.assertTrue(collection["redistribution"])
            self.assertEqual(collection["license"]["spdx"], "MIT")
            self.assertTrue(collection["original_publication"]["id"])
            self.assertEqual(collection["series_issue_url"],
                             "https://github.com/JunLin615/os-optics-lens/issues/36")
            self.assertEqual(record["native_project"]["engine_version"], "0.14.0")
            self.assertEqual(record["native_project"]["opening_outcome"], "ACCEPTED")

    def test_every_value_states_a_basis_from_the_vocabulary(self):
        for name, record in RECORDS.items():
            for quantity, fact in record["first_order"].items():
                if quantity == "engine_check":
                    continue
                self.assertIn(fact["basis"], BASES, f"{name}: first_order.{quantity}")
            for field_name, fact in record["aperture"].items():
                self.assertIn(fact["basis"], BASES, f"{name}: aperture.{field_name}")
            self.assertIn(record["field"]["basis"], BASES, name)
            for entry in record["approximations"]:
                self.assertTrue(entry["code"], name)
                self.assertTrue(entry["statement"], name)


class SpectrumTest(unittest.TestCase):
    def test_samples_span_and_primary_agree(self):
        for name, record in RECORDS.items():
            spectrum = record["spectrum"]
            samples = spectrum["sampling_wavelengths_nm"]
            self.assertTrue(samples, name)
            self.assertIn(spectrum["primary_wavelength_nm"], samples, name)
            self.assertEqual(len(spectrum["incident_weights"]), len(samples), name)
            span = spectrum["sampling_span_nm"]
            self.assertAlmostEqual(span["min_nm"], min(samples), places=6, msg=name)
            self.assertAlmostEqual(span["max_nm"], max(samples), places=6, msg=name)
            self.assertEqual(span["eligible_for"], "DISCOVERY_ONLY", name)
            expected = "SINGLE_WAVELENGTH_BAND" if len(samples) == 1 else "SPAN_OF_STATED_SAMPLING_WAVELENGTHS"
            self.assertEqual(span["basis"], expected, name)

    def test_air_values_are_kept_beside_the_vacuum_values(self):
        for name, record in RECORDS.items():
            air = record["spectrum"]["sampling_wavelengths_air_nm"]
            vacuum = record["spectrum"]["sampling_wavelengths_nm"]
            self.assertEqual(len(air), len(vacuum), name)
            for a, v in zip(air, vacuum):
                # air to vacuum is a small redshift, never a large one
                self.assertGreater(v, a, name)
                self.assertLess(v - a, 1.0, name)

    def test_coverage_is_never_claimed_from_sampling(self):
        for name, record in RECORDS.items():
            coverage = record["spectrum"]["continuous_coverage_nm"]
            self.assertEqual(coverage["state"], "UNCONFIRMED", name)
            self.assertIsNone(coverage["min_nm"], name)
            self.assertIsNone(coverage["max_nm"], name)

    def test_band_facet_contains_the_sampling_span(self):
        for name, record in RECORDS.items():
            spectrum = record["spectrum"]
            span = spectrum["sampling_span_nm"]
            facets = spectrum["band_facets"]
            self.assertTrue(facets, name)
            for facet in facets:
                low, high = BAND_FACETS[facet]
                self.assertGreaterEqual(span["min_nm"], low, f"{name}: {facet}")
                self.assertLessEqual(span["max_nm"], high, f"{name}: {facet}")


class FieldAndApertureTest(unittest.TestCase):
    def test_field_is_angular_and_the_full_field_is_twice_the_half_field(self):
        for name, record in RECORDS.items():
            field = record["field"]
            self.assertEqual(field["kind"], "ANGULAR", name)
            self.assertAlmostEqual(field["full_field_deg"], 2 * field["half_field_deg"], places=9, msg=name)
            for point in field.get("field_points_deg") or []:
                self.assertLessEqual(abs(point), field["half_field_deg"] + 1e-9, name)
            self.assertGreater(field["half_field_deg"], 0.0, name)

    def test_exactly_one_system_aperture(self):
        for name, record in RECORDS.items():
            marked = [k for k, v in record["aperture"].items() if v.get("system_aperture")]
            self.assertEqual(len(marked), 1, f"{name}: {marked}")

    def test_aperture_validity_is_consistent_with_the_project(self):
        for name, record in RECORDS.items():
            validity = record["design"]["aperture_validity"]
            self.assertIn(validity["state"], APERTURE_STATES, name)
            project = json.loads((ROOT / record["native_project"]["path"]).read_text(encoding="utf-8"))
            plates = [m for m in project["optical_models"]
                      if m["model"].get("model_kind") == "APERTURE_STOP"]
            if validity["state"] == "STOP_BLOCKING":
                self.assertTrue(plates, name)
                for plate in plates:
                    self.assertGreater(float(plate["model"]["edge_radius_mm"]),
                                       float(plate["model"]["opening_radius_mm"]), name)
                self.assertTrue(validity["stop_blocks_light"], name)
            else:
                self.assertFalse(validity["stop_blocks_light"], name)
            # no record in this batch claims a stop-dependent match
            self.assertFalse(validity["satisfies_stop_dependent_constraints"], name)


class ValidationTest(unittest.TestCase):
    def test_the_four_outcomes_use_the_documented_vocabulary(self):
        for name, record in RECORDS.items():
            validation = record["validation"]
            self.assertEqual(validation["conversion"], "PASSED", name)
            self.assertEqual(validation["project_check"], "ACCEPTED", name)
            self.assertEqual(validation["optical_solids"], "VALID", name)
            self.assertIn(validation["paraxial"], PARAXIAL_STATES, name)
            for finding in validation["solids_findings"]:
                self.assertIn(finding["severity"], SEVERITIES, name)
                self.assertTrue(finding["code"], name)
                self.assertTrue(finding["message"], name)

    def test_no_error_finding_survives_an_accepted_record(self):
        for name, record in RECORDS.items():
            errors = [f for f in record["validation"]["solids_findings"] if f["severity"] == "ERROR"]
            self.assertEqual(errors, [], name)

    def test_paraxial_match_implies_every_comparable_quantity_is_within_tolerance(self):
        """`MATCHED` means every quantity the source states was reproduced; an unstated one stays
        UNCONFIRMED and does not block the state, which is why the patent record can be MATCHED on
        its focal length while its back focus has no source value."""

        for name, record in RECORDS.items():
            comparison = record["first_order"]["engine_check"]["comparison"]
            if record["validation"]["paraxial"] != "MATCHED":
                continue
            compared = [q for q in ("effective_focal_length", "back_focal_length")
                        if "source_mm" in comparison[q]]
            self.assertTrue(compared, f"{name}: MATCHED with nothing compared")
            for quantity in compared:
                self.assertTrue(comparison[quantity].get("within_tolerance"), f"{name}: {quantity}")

    def test_the_comparison_arithmetic_recomputes(self):
        for name, record in RECORDS.items():
            comparison = record["first_order"]["engine_check"]["comparison"]
            self.assertAlmostEqual(comparison["atol_mm"], ATOL_MM, places=12, msg=name)
            self.assertAlmostEqual(comparison["rtol"], RTOL, places=12, msg=name)
            for quantity in ("effective_focal_length", "back_focal_length"):
                fact = comparison[quantity]
                if "state" in fact and fact["state"] == "UNCONFIRMED":
                    self.assertIsNotNone(fact["engine_mm"], name)
                    self.assertNotIn("source_mm", fact, name)
                    continue
                difference = abs(fact["engine_mm"] - fact["source_mm"])
                self.assertAlmostEqual(fact["absolute_difference_mm"], difference, places=9, msg=name)
                tolerance = comparison["atol_mm"] + comparison["rtol"] * fact["scale_mm"]
                self.assertAlmostEqual(fact["tolerance_mm"], tolerance, places=9, msg=name)
                self.assertEqual(fact["within_tolerance"], difference <= tolerance, f"{name}: {quantity}")
                self.assertAlmostEqual(fact["fraction_of_tolerance"], difference / tolerance, places=9,
                                       msg=name)
                self.assertLessEqual(difference, tolerance, f"{name}: {quantity} outside tolerance")

    def test_engine_check_repeats_the_first_order_values(self):
        for name, record in RECORDS.items():
            check = record["first_order"]["engine_check"]
            self.assertEqual(check["engine_version"], "0.14.0", name)
            self.assertAlmostEqual(check["effective_focal_length_mm"],
                                   record["first_order"]["effective_focal_length_mm"]["engine_value_mm"],
                                   places=9, msg=name)
            self.assertAlmostEqual(check["back_focal_length_mm"],
                                   record["first_order"]["back_focal_length_mm"]["value"], places=9, msg=name)


class ProjectGeometryTest(unittest.TestCase):
    """The Project has to look like a lens, not like a beam tube."""

    @staticmethod
    def sag(radius, height):
        if radius is None:
            return 0.0
        radius = float(radius)
        if abs(radius) <= abs(height):
            raise AssertionError(f"the outline {height} mm lies outside a sphere of radius {radius} mm")
        return radius - math.copysign(1.0, radius) * math.sqrt(radius * radius - height * height)

    def test_each_element_has_one_clear_diameter(self):
        for name, record in RECORDS.items():
            project = json.loads((ROOT / record["native_project"]["path"]).read_text(encoding="utf-8"))
            for model in project["optical_models"]:
                body = model["model"]
                if body.get("model_kind") != "SURFACE_SEQUENCE":
                    continue
                clears = {round(float(s["clear_radius_mm"]), 9) for s in body["surfaces"]}
                self.assertEqual(len(clears), 1, f"{name}: {model['model_id']} has clear radii {clears}")
                self.assertAlmostEqual(float(body["edge_radius_mm"]), clears.pop(), places=9, msg=name)

    def test_every_glass_gap_has_positive_edge_thickness(self):
        for name, record in RECORDS.items():
            project = json.loads((ROOT / record["native_project"]["path"]).read_text(encoding="utf-8"))
            for model in project["optical_models"]:
                body = model["model"]
                if body.get("model_kind") != "SURFACE_SEQUENCE":
                    continue
                surfaces = body["surfaces"]
                for front, back in zip(surfaces, surfaces[1:]):
                    centre = float(back["vertex_z_mm"]) - float(front["vertex_z_mm"])
                    height = float(body["edge_radius_mm"])
                    edge = centre + self.sag(back.get("radius_mm"), height) - self.sag(front.get("radius_mm"), height)
                    self.assertGreater(centre, 0.0, f"{name}: {model['model_id']} {front['surface_id']}")
                    self.assertGreater(edge, 0.0,
                                       f"{name}: {model['model_id']} {front['surface_id']}->{back['surface_id']}")

    def test_every_element_is_inside_the_compound_envelope(self):
        for name, record in RECORDS.items():
            project = json.loads((ROOT / record["native_project"]["path"]).read_text(encoding="utf-8"))
            envelope = next(m["model"]["envelope"] for m in project["optical_models"]
                            if m["model"].get("model_kind") == "COMPOUND")
            self.assertGreater(float(envelope["diameter_mm"]), 0.0, name)
            for model in project["optical_models"]:
                body = model["model"]
                if body.get("model_kind") == "SURFACE_SEQUENCE":
                    self.assertLessEqual(2 * float(body["edge_radius_mm"]),
                                         float(envelope["diameter_mm"]) + 1e-6, name)
                elif body.get("model_kind") == "APERTURE_STOP":
                    self.assertLessEqual(2 * float(body["edge_radius_mm"]),
                                         float(envelope["diameter_mm"]) + 1e-6, name)


class FamilyTest(unittest.TestCase):
    def test_the_handbook_eyepieces_share_one_scale_and_one_pupil(self):
        handbook = {k: v for k, v in RECORDS.items() if k.startswith("mil-hdbk-141-")}
        self.assertEqual(len(handbook), 9)
        fields = []
        for name, record in handbook.items():
            self.assertEqual(record["first_order"]["nominal_effective_focal_length_mm"]["value"], 25.4, name)
            self.assertEqual(record["aperture"]["entrance_pupil_diameter_mm"]["value"], 5.0, name)
            self.assertEqual(record["design"]["design_type"], "eyepiece", name)
            fields.append(record["field"]["full_field_deg"])
        self.assertEqual(fields, sorted(fields), "the family is ordered by field")

    def test_family_ids_group_without_merging(self):
        by_family = {}
        for name, record in RECORDS.items():
            by_family.setdefault(record["design"]["design_family_id"], []).append(name)
        for family, members in by_family.items():
            self.assertTrue(family, members)
            for member in members:
                self.assertEqual(RECORDS[member]["record_id"], member)


@unittest.skipUnless(ENGINE, "no os-optics executable: set OS_OPTICS_BIN to verify the optics")
class EngineTest(unittest.TestCase):
    def test_the_engine_accepts_every_project(self):
        for name, record in RECORDS.items():
            project = str(ROOT / record["native_project"]["path"])
            self.assertEqual(run_engine("project", "check", project)["status"], "ACCEPTED", name)
            solids = run_engine("project", "solids", project)
            self.assertEqual(solids["status"], "VALID", name)
            self.assertEqual([i for i in solids["issues"] if i["severity"] == "ERROR"], [], name)

    def test_the_engine_reproduces_the_recorded_first_order_values(self):
        for name, record in RECORDS.items():
            check = record["first_order"]["engine_check"]
            paraxial = run_engine("project", "paraxial", str(ROOT / record["native_project"]["path"]),
                                  "--components", record["native_project"]["instance_id"],
                                  "--wavelength-nm", repr(check["wavelength_nm"]))
            system = paraxial["system"]
            self.assertAlmostEqual(system["efl_mm"], check["effective_focal_length_mm"], places=6, msg=name)
            self.assertAlmostEqual(system["bfl_mm"], check["back_focal_length_mm"], places=6, msg=name)

    def test_the_recorded_solids_findings_are_the_engine_s(self):
        for name, record in RECORDS.items():
            solids = run_engine("project", "solids", str(ROOT / record["native_project"]["path"]))
            recorded = [(f["code"], f["severity"]) for f in record["validation"]["solids_findings"]]
            reported = [(i["code"], i["severity"]) for i in solids["issues"]]
            self.assertEqual(sorted(recorded), sorted(reported), name)


if __name__ == "__main__":
    unittest.main()

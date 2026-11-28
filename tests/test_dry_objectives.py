"""Checks for the dry-microscope-objective batch that need no engine.

Every committed number that a reviewer can recompute is recomputed here: the record hash, the
Project and evidence hashes, the prescription hash the record pins, the paraxial comparison verdict,
the family grouping and the reconciliation of the batch manifest with the record directories.
"""
from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
COLLECTION = REPO / "records" / "dry-microscope-objectives"
BATCH = COLLECTION / "batch-dry-microscope-objectives-1.json"

RECORD_HASH_METHOD = ("SHA-256 over parsed JSON with record_hash and record_hash_method removed: "
                      "json.dumps(body, sort_keys=True, ensure_ascii=False) encoded as UTF-8, no trailing newline.")


def record_dirs():
    return sorted(p for p in COLLECTION.iterdir() if p.is_dir() and (p / "record.json").exists())


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


class RecordFiles(unittest.TestCase):
    def test_every_record_directory_has_its_three_files(self):
        dirs = record_dirs()
        self.assertTrue(dirs, "no record directories found")
        for d in dirs:
            for name in ("project.json", "record.json", "evidence.json"):
                self.assertTrue((d / name).exists(), f"{d.name} is missing {name}")

    def test_record_hash_recomputes(self):
        for d in record_dirs():
            rec = load(d / "record.json")
            body = {k: v for k, v in rec.items() if k not in ("record_hash", "record_hash_method")}
            digest = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
            self.assertEqual(digest, rec["record_hash"], f"{d.name}: record_hash does not recompute")
            self.assertEqual(rec["record_hash_method"], RECORD_HASH_METHOD)

    def test_pinned_hashes_match_the_committed_bytes(self):
        for d in record_dirs():
            rec = load(d / "record.json")
            project = (d / "project.json").read_bytes().replace(b"\r\n", b"\n")
            evidence = (d / "evidence.json").read_bytes().replace(b"\r\n", b"\n")
            self.assertEqual(hashlib.sha256(project).hexdigest(), rec["native_project"]["sha256"],
                             f"{d.name}: Project hash")
            self.assertEqual(hashlib.sha256(evidence).hexdigest(),
                             rec["collection"]["evidence_extract"]["sha256"], f"{d.name}: evidence hash")

    def test_source_input_hash_matches_the_transcription(self):
        for d in record_dirs():
            rec = load(d / "record.json")
            ev = load(d / "evidence.json")
            text = ev["sections"]["prescription"]["text"].encode("utf-8")
            self.assertEqual(hashlib.sha256(text).hexdigest(), rec["collection"]["source_input_sha256"],
                             f"{d.name}: the transcription hash does not match the evidence extract")

    def test_paths_point_at_the_committed_files(self):
        for d in record_dirs():
            rec = load(d / "record.json")
            self.assertEqual(rec["native_project"]["path"], f"records/dry-microscope-objectives/{d.name}/project.json")
            self.assertEqual(rec["collection"]["evidence_extract"]["path"],
                             f"records/dry-microscope-objectives/{d.name}/evidence.json")


class RecordSemantics(unittest.TestCase):
    def test_accepted_records_hold_all_four_states(self):
        for d in record_dirs():
            v = load(d / "record.json")["validation"]
            self.assertEqual(v["conversion"], "PASSED", d.name)
            self.assertEqual(v["project_check"], "ACCEPTED", d.name)
            self.assertEqual(v["optical_solids"], "VALID", d.name)
            self.assertEqual(v["paraxial"], "MATCHED", d.name)

    def test_paraxial_verdict_recomputes_from_the_stored_numbers(self):
        for d in record_dirs():
            cmp = load(d / "record.json")["first_order"]["engine_check"]["comparison"]
            efl = cmp["effective_focal_length"]
            diff = abs(efl["engine_mm"] - efl["source_mm"])
            tol = cmp["atol_mm"] + cmp["rtol"] * abs(efl["scale_mm"])
            self.assertAlmostEqual(diff, efl["absolute_difference_mm"], places=9, msg=d.name)
            self.assertAlmostEqual(tol, efl["tolerance_mm"], places=9, msg=d.name)
            self.assertEqual(diff <= tol, efl["within_tolerance"], d.name)
            self.assertAlmostEqual(diff / tol, efl["fraction_of_tolerance"], places=9, msg=d.name)
            self.assertLessEqual(efl["fraction_of_tolerance"], 1.0, d.name)

    def test_a_record_never_claims_a_stop_dependent_match(self):
        for d in record_dirs():
            av = load(d / "record.json")["design"]["aperture_validity"]
            self.assertFalse(av["satisfies_stop_dependent_constraints"], d.name)
            if av["state"] == "NO_STOP":
                self.assertFalse(av["eligible_for_stop_dependent_evaluation"], d.name)

    def test_a_record_never_claims_continuous_coverage(self):
        for d in record_dirs():
            cov = load(d / "record.json")["spectrum"]["continuous_coverage_nm"]
            self.assertEqual(cov["state"], "UNCONFIRMED", d.name)
            self.assertIsNone(cov["min_nm"], d.name)
            self.assertIsNone(cov["max_nm"], d.name)

    def test_every_record_names_its_publication_and_table(self):
        for d in record_dirs():
            rec = load(d / "record.json")
            pub = rec["collection"]["original_publication"]
            self.assertTrue(pub["id"].startswith(("US", "EP", "WO", "CN", "JP")), d.name)
            self.assertTrue(pub["reference"], d.name)
            self.assertTrue(rec["collection"]["source_input"].startswith(pub["id"]), d.name)

    def test_derived_values_say_they_are_derived(self):
        for d in record_dirs():
            rec = load(d / "record.json")
            na = (rec.get("aperture") or {}).get("object_numerical_aperture")
            if na and na["basis"] == "DERIVED":
                self.assertIn("derived", na["definition"].lower(), d.name)
            codes = {a["code"] for a in rec["approximations"]}
            ev = load(d / "evidence.json")
            if ev["sections"]["derivation"]["clear_apertures"].startswith("DERIVED"):
                self.assertIn("CLEAR_APERTURES_DERIVED", codes, d.name)


class FamilyAndManifest(unittest.TestCase):
    def test_family_membership_groups_without_merging(self):
        fams = {}
        for d in record_dirs():
            rec = load(d / "record.json")
            fams.setdefault(rec["design"]["design_family_id"], []).append(rec["record_id"])
        self.assertIn("us11067782b2", fams)
        self.assertEqual(len(fams["us11067782b2"]), 8)
        for ids in fams.values():
            self.assertEqual(len(ids), len(set(ids)))

    def test_manifest_reconciles_with_the_record_directories(self):
        manifest = load(BATCH)
        on_disk = sorted(d.name for d in record_dirs())
        self.assertEqual(sorted(manifest["accepted_record_ids"]), on_disk)
        self.assertEqual(manifest["counts"]["accepted"], len(on_disk))
        self.assertEqual(manifest["source_count"], len(manifest["outcomes"]))
        outcomes = [r["outcome"] for r in manifest["outcomes"]]
        self.assertEqual(len(outcomes), manifest["counts"]["inputs"])
        self.assertEqual(outcomes.count("ACCEPTED"), manifest["counts"]["accepted"])
        self.assertEqual(outcomes.count("DEFERRED"), manifest["counts"]["deferred"])
        self.assertEqual(outcomes.count("CONVERSION_FAILED"), manifest["counts"]["conversion_failed"])
        for row in manifest["outcomes"]:
            if row["outcome"] == "ACCEPTED":
                self.assertIn(row["record_id"], on_disk)
            else:
                self.assertTrue(row["reason_code"])
                self.assertTrue(row["reason"])

    def test_manifest_focal_lengths_match_the_records(self):
        manifest = load(BATCH)
        for row in manifest["outcomes"]:
            if row["outcome"] != "ACCEPTED":
                continue
            rec = load(COLLECTION / row["record_id"] / "record.json")
            self.assertAlmostEqual(row["effective_focal_length_mm"],
                                   rec["first_order"]["engine_check"]["effective_focal_length_mm"], places=6)


if __name__ == "__main__":
    unittest.main()

"""Production CLI coverage of the Project-only retrieval projection."""

import json
import sqlite3
import tempfile
from pathlib import Path

from os_optics_lens.build import build_index
from os_optics_lens.canonical import sha256_hex
from test_acceptance_review import DUMP_RECORD, EFL, P2B, CliTestCase, helpers, setUpModule, tearDownModule

BFL = "first_order.back_focal_length_mm"
WAVELENGTH = 587.7220645720764


class ProjectFactCli(CliTestCase):
    def test_lenslibrary_engine_value_everywhere(self):
        cases = (
            (EFL, 239.89338, 239.89335686784239, "DOES_NOT_MATCH"),
            (BFL, 222.915802, 222.9158040539034, "MATCH"),
        )
        for key, threshold, value, outcome in cases:
            with self.subTest(fact=key):
                member = key.split(".")[1]
                candidate = self.candidate(DUMP_RECORD, constraints=[
                    {"id": "shorthand", "fact": key, "op": "gte", "value": threshold},
                    {"id": "expanded", "scope": ["first_order", member],
                     "conditions": {"wavelength_nm": WAVELENGTH},
                     "predicates": [{"path": "value", "op": "gte", "value": threshold}]},
                ])
                for result in candidate["constraints"].values():
                    self.assertEqual(result["outcome"], outcome)
                    self.assertEqual(result["fact"]["value"], value)
                    self.assertEqual(result["fact"]["conditions"], {"wavelength_nm": WAVELENGTH})
                self.assertEqual(candidate["facts"][key]["value"], value)
                fields = self.cli_ok("fields", "--fact", key, "--record-id", DUMP_RECORD)["result"]
                self.assertEqual((fields["min"], fields["max"]), (value, value))
                narrowed = self.cli_ok("facets", "--record-id", DUMP_RECORD,
                                       "--range", f"{key}={threshold}:")["result"]
                self.assertEqual(narrowed["records"], 1 if outcome == "MATCH" else 0)
                compared = self.cli_ok("compare", DUMP_RECORD, "--fields", key)["result"]
                self.assertEqual(compared["rows"][0]["values"][0]["value"], value)
                ranked = self.candidate(DUMP_RECORD, mode="starting_point",
                                        targets=[{"fact": key, "target": threshold, "tolerance": 1}],
                                        preferences=[{"fact": key, "direction": "ascending"}])
                self.assertEqual(ranked["shortfalls"][0]["value"], value)
                self.assertIn([0, value], ranked["rank"]["key"])

    def test_source_values_do_not_reach_predicates_or_discovery(self):
        candidate = self.candidate(DUMP_RECORD, constraints=[
            {"id": "identity", "scope": ["collection"],
             "predicates": [{"path": "collection_id", "op": "eq", "value": "lenslibrary"}]},
            {"id": "source", "scope": ["collection"],
             "predicates": [{"path": "source", "op": "eq", "value": "Nathan Hagen's LensLibrary"}]},
            {"id": "collection_object", "scope": ["collection"],
             "predicates": [{"path": "", "op": "eq", "value": {"collection_id": "lenslibrary"}}]},
            {"id": "dump_aperture", "fact": "aperture.working_f_number", "op": "gte", "value": 7},
        ])
        self.assertEqual({key: value["outcome"] for key, value in candidate["constraints"].items()}, {
            "identity": "MATCH", "source": "UNCONFIRMED", "collection_object": "MATCH",
            "dump_aperture": "UNCONFIRMED",
        })
        self.cli_error("UNKNOWN_FACET", 2, "facets", "--facet", "validation")
        connection = sqlite3.connect(self.index_path())
        self.addCleanup(connection.close)
        view, metadata = connection.execute(
            "SELECT view_json, record_json FROM configurations JOIN records USING (record_pk) WHERE record_id=?",
            (DUMP_RECORD,),
        ).fetchone()
        self.assertNotIn("engine_check", view)
        self.assertNotIn("239.8934", view + metadata)
        self.assertNotIn("prescription_dump_check", view + metadata)
        self.assertNotIn("paraxial_matched", view + metadata)
        self.assertNotIn("Nathan Hagen", view)

    def test_first_order_section_contains_only_project_values(self):
        result = self.cli_ok("inspect", DUMP_RECORD, "--sections", "first_order", "--fields", EFL)["result"]
        first_order = result["sections"]["first_order"]
        self.assertNotIn("engine_check", first_order)
        self.assertEqual(first_order[EFL.split(".")[1]]["value"], 239.89335686784239)
        self.assertEqual(first_order["total_track_mm"]["value"], 261.417804)
        self.assertNotIn("checks", result["fields"][EFL])

    def test_brief_pixel_pitch_is_unconfirmed_for_continuous_receiver(self):
        candidate = self.candidate(P2B, {"id": "pitch", "fact": "image_format.pixel_pitch_um",
                                         "op": "eq", "value": [12, 12]})
        self.assertEqual(candidate["constraints"]["pitch"]["outcome"], "UNCONFIRMED")

    def test_legacy_derivation_and_stale_analysis_are_unconfirmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            corpus = helpers.SynthCorpus(Path(tmp))
            record = corpus.add("stale")
            directory = corpus.record_dir("synth", "stale")
            extract = directory / "evidence.json"
            document = json.loads(extract.read_bytes())
            document.update(record_id="stale", record_revision=record["record_revision"] + 1)
            data = json.dumps(document).encode()
            extract.write_bytes(data)
            record["performance"][0]["source"]["sha256"] = sha256_hex(data)
            record["collection"]["evidence_extract"] = {
                "path": "records/synth/stale/evidence.json", "sha256": sha256_hex(data),
            }
            record["first_order"]["total_track_mm"] = {"value": 41, "basis": "DERIVED"}
            corpus.rewrite("synth", "stale", record)
            index = Path(tmp) / "index.sqlite"
            build_index(Path(tmp), index)
            candidate = self.candidate("stale", index=index, constraints=[
                {"id": "track", "fact": "first_order.total_track_mm", "op": "gt", "value": 0},
                {"id": "mtf", "fact": "performance.mtf_field_minimum", "op": "gt", "value": 0},
            ])
            for constraint in candidate["constraints"].values():
                self.assertEqual((constraint["outcome"], constraint["reason"]), ("UNCONFIRMED", "FACT_MISSING"))
            self.cli_error("EVIDENCE_SECTION_NOT_FOUND", 3, "inspect", "stale", "--evidence", "mtf", index=index)

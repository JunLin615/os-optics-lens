"""Constraint semantics through the production path (index views + query.search).

test_evaluate_unit.py checks the pure evaluator over raw records; this file checks the same rules
after normalization, indexing and the query layer's evaluation context (SPEC section 6.2-6.3,
amendments 10.1-10.3, 10.11, section 9 `test_semantics`).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from os_optics_lens import query
from os_optics_lens.build import build_index
from os_optics_lens.store import open_index

ROOT = Path(__file__).resolve().parents[1]
P2B = "yutong-2025-mirrorless-18-p2b"
SONNAR = "portrait-85-sonnar-eight"
ALL = ["MATCH", "UNCONFIRMED", "DOES_NOT_MATCH"]

_TMP: tempfile.TemporaryDirectory | None = None
INDEX = None


def setUpModule() -> None:
    global _TMP, INDEX
    _TMP = tempfile.TemporaryDirectory()
    build_index(ROOT, Path(_TMP.name) / "index.sqlite")
    INDEX = open_index(Path(_TMP.name) / "index.sqlite")


def tearDownModule() -> None:
    if INDEX is not None:
        INDEX.close()
    if _TMP is not None:
        _TMP.cleanup()


def evaluate(record_id: str, constraint: dict, detail: str = "full") -> tuple[dict, dict]:
    """(constraint entry, candidate) for one constraint on one record."""
    constraint = dict(constraint)
    constraint.setdefault("id", "k")
    request = {"mode": "specification", "filters": {"record_ids": [record_id]}, "constraints": [constraint],
               "include": ALL, "detail": detail}
    [candidate] = query.search(INDEX, request)["result"]["candidates"]
    entry = candidate["constraints"][constraint["id"]]
    if isinstance(entry, str):
        entry = {"outcome": entry}
    return entry, candidate


class StopGates(unittest.TestCase):
    def test_source_f_number_is_unconfirmed_despite_blocking_stop(self) -> None:
        for limit in (4, 5):
            entry, candidate = evaluate("lenslibrary-1792917", {
                "fact": "aperture.working_f_number", "op": "lte", "value": limit,
            })
            self.assertEqual((entry["outcome"], entry["reason"]), ("UNCONFIRMED", "FACT_MISSING"))
            self.assertEqual(candidate["shortfalls"], [])

    def test_paraxial_working_convention(self) -> None:
        entry, _ = evaluate("lenslibrary-6744570a",
                            {"fact": "aperture.paraxial_working_f_number", "op": "lte", "value": 4})
        self.assertEqual(entry["reason"], "FACT_MISSING")
        entry, _ = evaluate("lenslibrary-6744570a", {"fact": "aperture.f_number", "op": "lte", "value": 5})
        self.assertEqual((entry["outcome"], entry["reason"]), ("UNCONFIRMED", "FACT_MISSING"))

    def test_nonblocking_stop(self) -> None:
        for fact, op, value in (("aperture.image_space_f_number", "lte", 3),
                                ("aperture.entrance_pupil_diameter_mm", "gte", 0.4)):
            with self.subTest(fact=fact):
                entry, _ = evaluate("lenslibrary-7821720c", {"fact": fact, "op": op, "value": value})
                self.assertEqual((entry["outcome"], entry["reason"]), ("UNCONFIRMED", "STOP_NOT_ELIGIBLE"))


class PathGuards(unittest.TestCase):
    def test_discovery_span_guard(self) -> None:
        guard = {"scope": ["spectrum"], "predicates": [
            {"path": "sampling_span_nm.min_nm", "op": "lte", "value": 490},
            {"path": "sampling_span_nm.max_nm", "op": "gte", "value": 650}]}
        for record_id in (P2B, SONNAR, "lenslibrary-1792917"):
            with self.subTest(record=record_id):
                entry, _ = evaluate(record_id, guard)
                self.assertEqual((entry["outcome"], entry["reason"]), ("UNCONFIRMED", "DISCOVERY_ONLY"))

    def test_stop_prefix_guard(self) -> None:
        guard = {"scope": ["aperture"], "predicates": [{"path": "working_f_number.value", "op": "lte", "value": 3}]}
        entry, _ = evaluate("lenslibrary-7821720c", guard)
        self.assertEqual(entry["outcome"], "UNCONFIRMED")
        self.assertEqual(entry["reason"], "STOP_NOT_ELIGIBLE")

    def test_discovery_span_cannot_satisfy_hard_coverage(self) -> None:
        forms = (
            {"fact": "spectrum.sampling_span_nm", "op": "covers_interval", "value": [490, 650]},
            {"fact": "spectrum.sampling_span_nm", "path": "min_nm", "op": "lte", "value": 490},
            {"scope": ["spectrum", "sampling_span_nm"],
             "predicates": [{"path": "", "op": "covers_interval", "value": [490, 650]}]},
            {"fact": "spectrum.continuous_coverage_nm", "op": "covers_interval", "value": [490, 650]},
        )
        for constraint in forms:
            with self.subTest(constraint=constraint):
                entry, _ = evaluate(P2B, constraint)
                self.assertEqual(entry["outcome"], "UNCONFIRMED")
                self.assertIn(entry["reason"], ("DISCOVERY_ONLY", "COVERAGE_UNCONFIRMED"))
        # The discovery filter narrows, but never produces a constraint outcome.
        response = query.search(INDEX, {"mode": "specification", "filters": {"spectral_span": [490, 650]},
                                        "constraints": [{"fact": "first_order.effective_focal_length_mm",
                                                         "op": "gt", "value": 0}], "limit": 50,
                                        "max_output_bytes": 262144})
        for candidate in response["result"]["candidates"]:
            self.assertEqual(list(candidate["constraints"]), ["c0"])

    def test_fixture_preference_is_guarded_like_shorthand(self) -> None:
        # Amendment 10.9: the fixture form {scope, metric} never ranks by a discovery-only span.
        base = {"mode": "starting_point", "filters": {"collections": ["yutong-cup", "opensci-design-studies"]},
                "limit": 50}
        forms = ({"scope": ["spectrum", "sampling_span_nm"], "metric": "max_nm", "direction": "descending"},
                 {"fact": "spectrum.sampling_span_nm.max_nm", "direction": "descending"})
        for preference in forms:
            with self.subTest(preference=preference):
                candidates = query.search(INDEX, dict(base, preferences=[preference]))["result"]["candidates"]
                self.assertTrue(candidates)
                for candidate in candidates:
                    self.assertEqual(candidate["rank"]["key"][4], [1, None])
                    [unknown] = [u for u in candidate["unknowns"] if "preference" in u]
                    self.assertEqual(unknown["reason"], "DISCOVERY_ONLY")

    def test_property_list_comparison_is_never_a_value(self) -> None:
        # Comparison-only checks (RECORD_FORMAT.md, LENSLIBRARY_IMPORT.md) never satisfy or refute a
        # constraint, whatever the stop state: 7821720c's stop is not eligible, 6016226 stores an
        # object height (1.673) under its angular field.
        cases = (
            ("lenslibrary-7821720c", {"fact": "aperture.property_list_comparison", "op": "lte", "value": 3}),
            ("lenslibrary-7821720c", {"fact": "aperture.property_list_comparison", "op": "gt", "value": 3}),
            ("lenslibrary-7821720c", {"scope": ["aperture"], "predicates": [
                {"path": "property_list_comparison.value", "op": "lte", "value": 3}]}),
            ("lenslibrary-7821720c", {"fact": "field.property_list_comparison", "op": "gte", "value": 100}),
            ("lenslibrary-6016226", {"fact": "field.property_list_comparison", "op": "lte", "value": 2}),
            ("lenslibrary-6016226", {"scope": ["field", "property_list_comparison"],
                                     "predicates": [{"path": "value", "op": "lte", "value": 2}]}),
            ("lenslibrary-1792917", {"fact": "first_order.engine_check", "op": "ne", "value": None}),
        )
        for record_id, constraint in cases:
            with self.subTest(record=record_id, constraint=constraint):
                entry, candidate = evaluate(record_id, constraint)
                self.assertEqual((entry["outcome"], entry["reason"]), ("UNCONFIRMED", "COMPARISON_ONLY"))
                self.assertEqual(candidate["outcome"], "UNCONFIRMED")
        real, _ = evaluate("lenslibrary-6016226", {"fact": "field.half_field_deg", "op": "lte", "value": 2})
        self.assertEqual((real["outcome"], real["reason"]), ("UNCONFIRMED", "FACT_MISSING"))

    def test_whole_sections_compare_project_members_and_keep_guards(self) -> None:
        record_id = "lenslibrary-7821720c"
        projected = query.inspect(INDEX, record_id, sections=["aperture"])["result"]["sections"]["aperture"]
        original = json.loads((ROOT / "records/lenslibrary" / record_id / "record.json").read_bytes())["aperture"]
        self.assertEqual(set(projected), {"stop_opening_diameter_mm"})
        for value, outcome in ((projected, "MATCH"), (original, "DOES_NOT_MATCH")):
            entry, _ = evaluate(record_id, {"scope": ["aperture"], "predicates": [
                {"path": "", "op": "eq", "value": value}]})
            self.assertEqual(entry["outcome"], outcome)
        # A whole spectrum object contains the discovery-only sampling span.
        entry, _ = evaluate(P2B, {"scope": ["spectrum"], "predicates": [{"path": "", "op": "ne", "value": {}}]})
        self.assertEqual((entry["outcome"], entry["reason"]), ("UNCONFIRMED", "DISCOVERY_ONLY"))


class Conventions(unittest.TestCase):
    def test_aperture_conventions_never_substitute(self) -> None:
        entry, _ = evaluate(P2B, {"fact": "aperture.f_number", "op": "lte", "value": 2})
        self.assertEqual((entry["outcome"], entry["reason"]), ("UNCONFIRMED", "FACT_MISSING"))
        alternatives = {alt["fact"] for alt in entry["details"]["available_alternatives"]}
        self.assertIn("aperture.entrance_pupil_f_number", alternatives)

    def test_geometric_vs_paraxial_back_focus(self) -> None:
        geometric = {"distance_kind": "geometric_axial_distance", "to": "fixed_image_plane"}
        entry, _ = evaluate(P2B, {"fact": "first_order.final_powered_vertex_to_sensor_mm", "op": "gte",
                                  "value": 16.5, "conditions": geometric})
        self.assertEqual(entry["outcome"], "MATCH")
        entry, _ = evaluate(P2B, {"fact": "first_order.back_focal_length_mm", "op": "gte", "value": 0.5,
                                  "conditions": geometric})
        self.assertEqual((entry["outcome"], entry["reason"]), ("UNCONFIRMED", "CONDITIONS_DIFFER"))

    def test_incomplete_evidence(self) -> None:
        constraint = {"fact": "first_order.effective_focal_length_mm", "op": "gt", "value": 0,
                      "requires_evidence_source": True}
        entry, _ = evaluate("lenslibrary-1792917", constraint)
        self.assertEqual((entry["outcome"], entry["reason"]), ("UNCONFIRMED", "EVIDENCE_MISSING"))
        entry, _ = evaluate(P2B, {"fact": "first_order.final_powered_vertex_to_sensor_mm", "op": "gt",
                                  "value": 0, "requires_evidence_source": True})
        self.assertEqual(entry["outcome"], "MATCH")
        self.assertEqual(entry["fact"]["source"]["status"], "RESOLVED")


class Shorthand(unittest.TestCase):
    def test_scalar_image_circle(self) -> None:
        entry, _ = evaluate(P2B, {"fact": "image_format.image_circle_diameter_mm", "op": "gte", "value": 43.27},
                            detail="compact")
        self.assertEqual(entry, {"outcome": "MATCH"})
        entry, candidate = evaluate(P2B, {"fact": "image_format.image_circle_diameter_mm", "op": "gte",
                                          "value": 44})
        self.assertEqual(entry["outcome"], "DOES_NOT_MATCH")
        self.assertAlmostEqual(candidate["shortfalls"][0]["shortfall"], 0.73, places=9)

    def test_unpinned(self) -> None:
        _, candidate = evaluate(P2B, {"fact": "performance.mtf_field_minimum", "op": "gt", "value": 0.5,
                                      "conditions": {"frequency_lp_mm": 40}}, detail="compact")
        entry = candidate["constraints"]["k"]
        self.assertEqual(entry["outcome"], "MATCH")
        self.assertIn("unpinned", entry)
        self.assertLessEqual(len(entry["unpinned"]), 16)
        self.assertNotIn("_more", entry["unpinned"])
        self.assertNotIn("frequency_lp_mm", entry["unpinned"])
        _, candidate = evaluate(P2B, {"fact": "first_order.effective_focal_length_mm", "op": "gt", "value": 0},
                                detail="compact")
        self.assertEqual(candidate["constraints"]["k"]["unpinned"], {"_text": "Primary wavelength 587.0 nm (vacuum)."})

    def test_select_on_sonnar_mtf(self) -> None:
        select = {"frequency_lp_mm": 10, "image_height_mm": 0, "direction": "tangential"}
        entry, _ = evaluate(SONNAR, {"fact": "performance.mtf", "op": "gt", "value": 0.9, "select": select})
        self.assertEqual(entry["outcome"], "MATCH")
        self.assertNotIn("source", entry["fact"])
        entry, _ = evaluate(SONNAR, {"fact": "performance.mtf", "op": "gt", "value": 0.9})
        self.assertEqual(entry["reason"], "AMBIGUOUS_FACTS")

    def test_source_ops_check_the_source_member(self) -> None:
        # The review reproduction: has_source on an object or metric fact named without a member
        # checks its source (has_sourced_method its methods), like the explicit `.source` form.
        for fact, op in (("performance.relative_illumination", "has_source"), ("cost_proxy", "has_source"),
                         ("performance.relative_illumination.source", "has_source"),
                         ("analysis_evidence.ghost", "has_sourced_method")):
            with self.subTest(fact=fact):
                entry, _ = evaluate(P2B, {"fact": fact, "op": op})
                self.assertEqual(entry["outcome"], "MATCH", entry)
                if op == "has_source":
                    self.assertEqual(entry["fact"]["source"]["status"], "RESOLVED")

    def test_shorthand_on_absent_fact_warns(self) -> None:
        response = query.search(INDEX, {"mode": "specification", "filters": {"record_ids": [P2B]},
                                        "constraints": [{"fact": "microscope.working_distance_mm", "op": "gte",
                                                         "value": 1}], "include": ALL})
        [candidate] = response["result"]["candidates"]
        self.assertEqual(candidate["constraints"]["c0"]["reason"], "FACT_MISSING")
        self.assertEqual([w["code"] for w in response["warnings"]], ["UNKNOWN_FIELD"])
        self.assertEqual(response["warnings"][0]["pointer"], "/constraints/0/fact")
        json.dumps(response)


if __name__ == "__main__":
    unittest.main()

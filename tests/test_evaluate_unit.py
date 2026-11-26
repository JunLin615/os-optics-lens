"""Unit tests for the pure evaluator (`os_optics_lens.evaluate`) over committed records.

Each 1.0 record is used directly as its configuration view (plus the `configuration` key). Evidence
resolves against this checkout's files with a resolver that implements amendment 10.4 over
LF-normalized bytes, so these tests need no index.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import math
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
try:
    from os_optics_lens import evaluate as ev
except ImportError:  # running without PYTHONPATH=src
    sys.path.insert(0, str(ROOT / "src"))
    from os_optics_lens import evaluate as ev

_spec = importlib.util.spec_from_file_location("_yutong_reference", ROOT / "tests/test_yutong_acceptance.py")
reference = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reference)

CASE = json.loads((ROOT / "acceptance/yutong-2025-mirrorless-problem-2.json").read_text(encoding="utf-8"))
CONSTRAINTS = {item["id"]: item for item in CASE["constraints"]}
RECORD_PATHS = sorted(ROOT.glob("records/*/*/record.json"))


def load_record(record_id: str) -> dict:
    path = next(p for p in RECORD_PATHS if p.parent.name == record_id)
    return json.loads(path.read_text(encoding="utf-8"))


def as_view(record: dict) -> dict:
    view = copy.deepcopy(record)
    view.pop("configurations", None)
    view["configuration"] = {"configuration_id": None, "label": "project as written", "native": False}
    return view


def resolve_source(source: object) -> dict:
    """Amendment 10.4 against the checkout: well-formed locator, path inside the root, LF-normalized
    sha256 equal, `sections[section]` present and not null."""
    if source is None:
        return {"status": "MISSING"}
    if not isinstance(source, dict):
        return {"status": "MALFORMED"}
    path, section, digest = (source.get(k) for k in ("path", "section", "sha256"))
    if not all(isinstance(v, str) and v for v in (path, section, digest)):
        return {"status": "MALFORMED"}
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        return {"status": "MALFORMED"}
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        return {"status": "PATH_ESCAPES"}
    resolved = (ROOT / path).resolve()
    if not resolved.is_relative_to(ROOT):
        return {"status": "PATH_ESCAPES"}
    if not resolved.is_file():
        return {"status": "FILE_MISSING"}
    payload = resolved.read_bytes().replace(b"\r\n", b"\n")
    if hashlib.sha256(payload).hexdigest() != digest:
        return {"status": "HASH_MISMATCH"}
    try:
        extract = json.loads(payload)
    except (UnicodeError, ValueError):
        return {"status": "MALFORMED"}
    sections = extract.get("sections") if isinstance(extract, dict) else None
    if not isinstance(sections, dict) or sections.get(section) is None:
        return {"status": "SECTION_MISSING"}
    return {"status": "RESOLVED"}


CTX = ev.EvalContext(
    stop_dependent=frozenset({"performance.mtf", "performance.mtf_field_minimum",
                              "performance.relative_illumination"}),
    stop_independent=frozenset({"aperture.stop_opening_diameter_mm", "aperture.property_list_comparison"}),
    aperture_default_stop_dependent=True,
    resolve_source=resolve_source,
)

P2B = load_record("yutong-2025-mirrorless-18-p2b")
SONNAR = load_record("portrait-85-sonnar-eight")

EXPECTED_REASONS = {
    "rear_focal_point_cannot_replace_sensor_distance": "CONDITIONS_DIFFER",
    "comparable_back_focus_below_limit": None,
    "single_mtf_point_is_not_a_sweep": "FACT_MISSING",
    "sweep_without_center": "SAMPLING_DIFFERS",
    "mtf_wavelength_weights_differ": "CONDITIONS_DIFFER",
    "mtf_limit_is_strict": None,
    "illumination_at_inner_field": "CONDITIONS_DIFFER",
    "illumination_at_wrong_image_circle": "CONDITIONS_DIFFER",
    "geometric_throughput_cannot_substitute_for_irradiance": "CONDITIONS_DIFFER",
    "illumination_with_wrong_normalization": "CONDITIONS_DIFFER",
    "missing_tolerance_source": "VALUE_MISSING",
    "missing_ghost_sources": "EVIDENCE_MISSING",
    "a_nonblocking_stop_cannot_confirm_a_pupil_based_match": "STOP_NOT_ELIGIBLE",
    "sweep_spanning_range_on_insufficient_grid": "SAMPLING_DIFFERS",
    "mtf_sweep_without_source": "EVIDENCE_MISSING",
    "corner_illumination_without_source": "EVIDENCE_MISSING",
    "corner_illumination_with_unresolvable_source": "EVIDENCE_UNRESOLVED",
}


def mutate(record: dict, case: dict) -> dict:
    """Apply a counterexample exactly as tests/test_yutong_acceptance.py does."""
    record = copy.deepcopy(record)
    constraint = CONSTRAINTS[case["constraint"]]
    scope_path = constraint["scope"]
    if "record_path" in case:
        *parent_path, key = case["record_path"]
        reference.at(record, parent_path)[key] = case["value"]
    elif case["path"]:
        scope = reference.at(record, scope_path)
        if "metric" in constraint:
            scope = next(entry for entry in scope if entry["metric"] == constraint["metric"])
        *parent_path, key = case["path"].split(".")
        parent = reference.at(scope, parent_path)
        if case.get("remove"):
            del parent[key]
        else:
            parent[key] = case["value"]
    else:
        parent = reference.at(record, scope_path[:-1])
        parent[scope_path[-1]] = reference.at(record, case["copy_from"])
    return record


def run(record: dict, constraint: dict, **kwargs) -> dict:
    return ev.evaluate_constraint(as_view(record), constraint, CTX, **kwargs)


class YutongFixture(unittest.TestCase):
    def test_all_constraints_match_on_p2b(self):
        result = ev.evaluate_configuration(as_view(P2B), CASE["constraints"], CTX, detail=True)
        self.assertEqual(result["outcome"], "MATCH")
        self.assertEqual(result["counts"], {"MATCH": 16, "UNCONFIRMED": 0, "DOES_NOT_MATCH": 0})
        by_id = {item["id"]: item for item in result["constraints"]}
        mtf = by_id["mtf_0_to_0p8"]["fact"]
        self.assertEqual(mtf["source"]["status"], "RESOLVED")
        self.assertEqual(mtf["json_path"], "performance[2]")
        self.assertEqual(mtf["unit"], "1")
        self.assertEqual(by_id["corner_relative_illumination"]["fact"]["source"]["status"], "RESOLVED")
        self.assertEqual(by_id["efl"]["fact"]["unit"], "mm")

    def test_unpinned_conditions(self):
        efl = run(P2B, CONSTRAINTS["efl"])
        self.assertEqual(efl["unpinned"], {"_text": "Primary wavelength 587.0 nm (vacuum)."})
        self.assertNotIn("unpinned", run(P2B, CONSTRAINTS["optical_back_focus"]))
        self.assertNotIn("unpinned", run(P2B, CONSTRAINTS["image_circle"]))
        partial = copy.deepcopy(CONSTRAINTS["cra"])
        del partial["conditions"]["angle_reference"]
        self.assertEqual(run(P2B, partial)["unpinned"], {"angle_reference": "image_plane_normal"})

    def test_counterexamples_outcomes_and_reasons(self):
        self.assertEqual(set(EXPECTED_REASONS), {case["id"] for case in CASE["counterexamples"]})
        for case in CASE["counterexamples"]:
            with self.subTest(case=case["id"]):
                record = mutate(P2B, case)
                constraint = CONSTRAINTS[case["constraint"]]
                result = run(record, constraint)
                self.assertEqual(result["outcome"], case["expected"])
                self.assertEqual(result["outcome"], reference.evaluate(record, constraint))
                self.assertEqual(result.get("reason"), EXPECTED_REASONS[case["id"]])

    def test_counterexample_details(self):
        cases = {case["id"]: case for case in CASE["counterexamples"]}
        below = run(mutate(P2B, cases["comparable_back_focus_below_limit"]), CONSTRAINTS["optical_back_focus"])
        (shortfall,) = below["shortfalls"]
        self.assertAlmostEqual(shortfall["shortfall"], 0.01, delta=1e-9)
        self.assertEqual(shortfall["limit"], 16.5)
        self.assertEqual(shortfall["unit"], "mm")
        strict = run(mutate(P2B, cases["mtf_limit_is_strict"]), CONSTRAINTS["mtf_0_to_0p8"])
        (shortfall,) = strict["shortfalls"]
        self.assertEqual(shortfall["shortfall"], 0.0)
        self.assertIsInstance(shortfall["shortfall"], float)
        self.assertIs(shortfall["strict"], True)
        missing = run(mutate(P2B, cases["single_mtf_point_is_not_a_sweep"]), CONSTRAINTS["mtf_0_to_0p8"])
        self.assertIn({"fact": "performance.mtf", "entries": 3},
                      missing["details"]["available_alternatives"])
        rear = run(mutate(P2B, cases["rear_focal_point_cannot_replace_sensor_distance"]),
                   CONSTRAINTS["optical_back_focus"])
        self.assertEqual(rear["details"]["differs"]["to"],
                         {"expected": "fixed_image_plane", "actual": "paraxial_rear_focal_point"})
        unresolved = run(mutate(P2B, cases["corner_illumination_with_unresolvable_source"]),
                         CONSTRAINTS["corner_relative_illumination"])
        self.assertEqual(unresolved["details"]["status"], "FILE_MISSING")

    def test_spectral_weights_compare_by_ratio(self):
        record = copy.deepcopy(P2B)
        record["spectrum"]["incident_weights"] = [2, 2, 2]
        for metric in ("mtf_field_minimum", "relative_illumination"):
            fact = next(fact for fact in record["performance"] if fact["metric"] == metric)
            fact["conditions"]["spectrum"]["incident_weights"] = [2, 2, 2]
        for name in ("spectral_sampling", "mtf_0_to_0p8", "corner_relative_illumination"):
            with self.subTest(constraint=name):
                self.assertEqual(run(record, CONSTRAINTS[name])["outcome"], "MATCH")
        record["spectrum"]["sampling_wavelengths_nm"].reverse()
        self.assertEqual(run(record, CONSTRAINTS["spectral_sampling"])["outcome"], "DOES_NOT_MATCH")

    def test_parity_with_reference(self):
        self.assertGreaterEqual(len(RECORD_PATHS), 41)
        exceptions = 0
        for path in RECORD_PATHS:
            record = json.loads(path.read_text(encoding="utf-8"))
            satisfies = record["design"].get("aperture_validity", {}).get(
                "satisfies_stop_dependent_constraints") is True
            for constraint in CASE["constraints"]:
                with self.subTest(record=record["record_id"], constraint=constraint["id"]):
                    expected = reference.evaluate(record, constraint)
                    result = run(record, constraint)
                    if result["outcome"] == expected:
                        continue
                    exceptions += 1
                    if result.get("reason") == "STOP_MATCH_NOT_SUPPORTED":
                        self.assertEqual(expected, "MATCH")
                        self.assertFalse(satisfies)
                    else:
                        self.assertEqual(result.get("reason"), "STOP_NOT_ELIGIBLE")
                        self.assertNotIn("requires_blocking_stop", constraint)
        self.assertEqual(exceptions, 0)


class StopGates(unittest.TestCase):
    def test_eligible_but_not_satisfying_record(self):
        record = load_record("lenslibrary-1792917")
        within = run(record, {"id": "w", "fact": "aperture.working_f_number", "op": "lte", "value": 5})
        self.assertEqual((within["outcome"], within["reason"]), ("UNCONFIRMED", "STOP_MATCH_NOT_SUPPORTED"))
        self.assertEqual(within["details"]["state"], "STOP_BLOCKING")
        self.assertIn("satisfaction_basis", within["details"])
        beyond = run(record, {"id": "w", "fact": "aperture.working_f_number", "op": "lte", "value": 4})
        self.assertEqual(beyond["outcome"], "DOES_NOT_MATCH")
        self.assertAlmostEqual(beyond["shortfalls"][0]["shortfall"], 0.511507, places=9)

    def test_paraxial_working_convention_record(self):
        record = load_record("lenslibrary-6744570a")
        declared = run(record, {"fact": "aperture.paraxial_working_f_number", "op": "lte", "value": 4})
        self.assertEqual(declared["reason"], "STOP_MATCH_NOT_SUPPORTED")
        self.assertEqual(declared["id"], "c0")
        absent = run(record, {"id": "f", "fact": "aperture.f_number", "op": "lte", "value": 5})
        self.assertEqual((absent["outcome"], absent["reason"]), ("UNCONFIRMED", "FACT_MISSING"))
        names = [alt["fact"] for alt in absent["details"]["available_alternatives"]]
        self.assertIn("aperture.paraxial_working_f_number", names)
        self.assertNotIn("aperture.property_list_comparison", names)

    def test_nonblocking_stop_is_not_eligible(self):
        record = load_record("lenslibrary-7821720c")
        for constraint in (
            {"id": "n", "fact": "aperture.image_space_f_number", "op": "lte", "value": 3},
            {"id": "d", "fact": "aperture.entrance_pupil_diameter_mm", "op": "gte", "value": 0.4},
        ):
            with self.subTest(fact=constraint["fact"]):
                result = run(record, constraint)
                self.assertEqual((result["outcome"], result["reason"]), ("UNCONFIRMED", "STOP_NOT_ELIGIBLE"))
                self.assertEqual(result["details"]["state"], "STOP_NOT_BLOCKING")

    def test_stop_independent_member_is_not_gated(self):
        record = copy.deepcopy(P2B)
        record["design"]["aperture_validity"] = {"state": "STOP_NOT_BLOCKING", "stop_blocks_light": False,
                                                 "eligible_for_stop_dependent_evaluation": False}
        result = run(record, {"id": "s", "fact": "aperture.stop_opening_diameter_mm", "op": "gte", "value": 20})
        self.assertEqual(result["outcome"], "MATCH")

    def test_path_guard_stop_prefix(self):
        guard = {"id": "b", "scope": ["aperture"],
                 "predicates": [{"path": "working_f_number.value", "op": "lte", "value": 3}]}
        record = load_record("lenslibrary-7821720c")
        self.assertEqual(reference.evaluate(record, guard), "MATCH")
        result = run(record, guard)
        self.assertEqual((result["outcome"], result["reason"]), ("UNCONFIRMED", "STOP_NOT_ELIGIBLE"))

    def test_aperture_conventions_never_substitute(self):
        result = run(P2B, {"id": "f", "fact": "aperture.f_number", "op": "lte", "value": 2})
        self.assertEqual((result["outcome"], result["reason"]), ("UNCONFIRMED", "FACT_MISSING"))
        alternatives = {alt["fact"]: alt for alt in result["details"]["available_alternatives"]}
        self.assertEqual(alternatives["aperture.entrance_pupil_f_number"]["value"], 1.9789)
        self.assertIn("aperture.stop_opening_diameter_mm", alternatives)


class SpectralCoverage(unittest.TestCase):
    def test_discovery_span_never_satisfies_coverage(self):
        guard = {"id": "a", "scope": ["spectrum"], "predicates": [
            {"path": "sampling_span_nm.min_nm", "op": "lte", "value": 490},
            {"path": "sampling_span_nm.max_nm", "op": "gte", "value": 650}]}
        self.assertEqual(reference.evaluate(P2B, guard), "MATCH")
        result = run(P2B, guard)
        self.assertEqual((result["outcome"], result["reason"]), ("UNCONFIRMED", "DISCOVERY_ONLY"))
        for constraint in (
            {"fact": "spectrum.sampling_span_nm", "op": "covers_interval", "value": [490, 650]},
            {"fact": "spectrum.sampling_span_nm", "path": "min_nm", "op": "lte", "value": 490},
            {"scope": ["spectrum", "sampling_span_nm"],
             "predicates": [{"path": "", "op": "covers_interval", "value": [490, 650]}]},
        ):
            with self.subTest(constraint=constraint):
                result = run(P2B, constraint)
                self.assertEqual((result["outcome"], result["reason"]), ("UNCONFIRMED", "DISCOVERY_ONLY"))

    def test_unconfirmed_coverage(self):
        for constraint in (
            {"fact": "spectrum.continuous_coverage_nm", "op": "covers_interval", "value": [490, 650]},
            {"fact": "spectrum.continuous_coverage_nm", "path": "min_nm", "op": "lte", "value": 490},
            {"scope": ["spectrum"], "predicates": [
                {"path": "continuous_coverage_nm.max_nm", "op": "gte", "value": 650}]},
        ):
            with self.subTest(constraint=constraint):
                result = run(P2B, constraint)
                self.assertEqual((result["outcome"], result["reason"]), ("UNCONFIRMED", "COVERAGE_UNCONFIRMED"))
                self.assertEqual(result["details"]["sampling_span_nm"], [486, 656])
                self.assertEqual(result["details"]["note"], "sampling span is discovery-only")
        state = run(P2B, {"fact": "spectrum.continuous_coverage_nm.state", "op": "eq", "value": "UNCONFIRMED"})
        self.assertEqual(state["outcome"], "MATCH")

    def test_confirmed_coverage(self):
        record = copy.deepcopy(P2B)
        record["spectrum"]["continuous_coverage_nm"] = {"state": "CONFIRMED", "min_nm": 400, "max_nm": 700}
        covered = run(record, {"fact": "spectrum.continuous_coverage_nm", "op": "covers_interval",
                               "value": [450, 650]})
        self.assertEqual(covered["outcome"], "MATCH")
        short = run(record, {"id": "cov", "fact": "spectrum.continuous_coverage_nm",
                             "op": "covers_interval", "value": [380, 650]})
        self.assertEqual(short["outcome"], "DOES_NOT_MATCH")
        (shortfall,) = short["shortfalls"]
        self.assertEqual(shortfall["shortfall"], 20.0)
        self.assertEqual(shortfall["limit"], [380, 650])
        self.assertEqual(shortfall["unit"], "nm")

    def test_two_sampled_wavelengths_are_not_coverage(self):
        # A dual-laser design samples 532 and 1064 nm; nothing is known between them, so the
        # sample list can never satisfy covers_interval (only a confirmed coverage object can).
        record = copy.deepcopy(P2B)
        record["spectrum"]["sampling_wavelengths_nm"] = [532, 1064]
        record["spectrum"]["sampling_span_nm"] = {"min_nm": 532, "max_nm": 1064, "eligible_for": "DISCOVERY_ONLY"}
        for constraint in (
            {"fact": "spectrum.sampling_wavelengths_nm", "op": "covers_interval", "value": [600, 1000]},
            {"scope": ["spectrum"], "predicates": [
                {"path": "sampling_wavelengths_nm", "op": "covers_interval", "value": [600, 1000]}]},
            {"scope": ["spectrum", "sampling_wavelengths_nm"],
             "predicates": [{"path": "", "op": "covers_interval", "value": [600, 1000]}]},
        ):
            with self.subTest(constraint=constraint):
                result = run(record, constraint)
                self.assertEqual((result["outcome"], result["reason"]), ("UNCONFIRMED", "VALUE_TYPE"))
                self.assertIn("includes_all", result["details"]["hint"])
        result = run(record, {"fact": "spectrum.sampling_wavelengths_nm", "op": "includes_all",
                              "value": [532, 1064]})
        self.assertEqual(result["outcome"], "MATCH")

    def test_sampled_wavelengths(self):
        result = run(P2B, {"fact": "spectrum.sampling_wavelengths_nm", "op": "includes_all", "value": [587, 656]})
        self.assertEqual(result["outcome"], "MATCH")
        result = run(P2B, {"fact": "spectrum.sampling_wavelengths_nm", "op": "includes_all", "value": [588]})
        self.assertEqual(result["outcome"], "DOES_NOT_MATCH")
        self.assertEqual(result["details"]["missing"], [588])
        result = run(P2B, {"fact": "spectrum.sampling_wavelengths_nm", "op": "includes_all",
                           "value": [587.5], "tolerance": 0.5})
        self.assertEqual(result["outcome"], "MATCH")


class Shorthand(unittest.TestCase):
    def test_scalar_member(self):
        fit = run(P2B, {"id": "ic", "fact": "image_format.image_circle_diameter_mm", "op": "gte", "value": 43.27})
        self.assertEqual(fit["outcome"], "MATCH")
        self.assertNotIn("unpinned", fit)
        short = run(P2B, {"id": "ic", "fact": "image_format.image_circle_diameter_mm", "op": "gte", "value": 44})
        self.assertEqual(short["outcome"], "DOES_NOT_MATCH")
        (shortfall,) = short["shortfalls"]
        self.assertAlmostEqual(shortfall["shortfall"], 0.73, places=9)
        self.assertEqual(shortfall["path"], "image_format.image_circle_diameter_mm")
        self.assertEqual(shortfall["unit"], "mm")
        self.assertAlmostEqual(shortfall["relative"], 0.73 / 44, places=12)

    def test_expansion_shapes(self):
        view = as_view(P2B)
        scalar = ev.expand_constraint({"fact": "image_format.width_mm", "op": "eq", "value": 36}, view, 3)
        self.assertEqual(scalar["id"], "c3")
        self.assertEqual(scalar["scope"], ["image_format"])
        self.assertEqual(scalar["predicates"], [{"op": "eq", "value": 36, "path": "width_mm"}])
        obj = ev.expand_constraint({"fact": "first_order.total_track_mm", "op": "lte", "value": 110}, view, 0)
        self.assertEqual(obj["scope"], ["first_order", "total_track_mm"])
        self.assertEqual(obj["predicates"][0]["path"], "value")
        metric = ev.expand_constraint({"fact": "performance.cra", "op": "lte", "value": 18}, view, 0)
        self.assertEqual((metric["scope"], metric["metric"]), (["performance"], "cra"))
        self.assertEqual(metric["predicates"][0]["path"], "value_deg")
        deferred = ev.expand_constraint({"fact": "performance.cra", "op": "lte", "value": 18}, None, 1)
        self.assertEqual(deferred["fact"], "performance.cra")
        self.assertNotIn("scope", deferred)
        self.assertEqual(run(P2B, deferred)["outcome"], "MATCH")
        with self.assertRaises(ev.RequestError) as raised:
            ev.expand_constraint({"fact": "performance.distortion_range", "op": "gte", "value": -2}, view, 0)
        self.assertEqual(raised.exception.code, "INVALID_REQUEST")
        self.assertIn("min_percent", raised.exception.hint)
        interval = run(P2B, {"fact": "performance.distortion_range", "path": "min_percent", "op": "gte", "value": -2})
        self.assertEqual(interval["outcome"], "MATCH")

    def test_fact_absent_from_view(self):
        result = run(P2B, {"id": "wd", "fact": "microscope.working_distance_mm", "op": "gte", "value": 1})
        self.assertEqual((result["outcome"], result["reason"]), ("UNCONFIRMED", "FACT_MISSING"))
        self.assertFalse(ev.fact_present(as_view(P2B), "microscope.working_distance_mm"))
        self.assertTrue(ev.fact_present(as_view(P2B), "performance.cra"))
        self.assertTrue(ev.fact_present(as_view(P2B), "image_format.width_mm"))

    def test_select_on_sonnar_mtf(self):
        select = {"frequency_lp_mm": 10, "image_height_mm": 0, "direction": "tangential"}
        result = run(SONNAR, {"id": "m", "fact": "performance.mtf", "op": "gt", "value": 0.9, "select": select},
                     detail=True)
        self.assertEqual(result["outcome"], "MATCH")
        self.assertEqual(result["fact"]["json_path"], "performance[0]")
        self.assertEqual(result["fact"]["value"], 0.9679991689193085)
        self.assertNotIn("source", result["fact"])
        ambiguous = run(SONNAR, {"id": "m", "fact": "performance.mtf", "op": "gt", "value": 0.9})
        self.assertEqual(ambiguous["reason"], "AMBIGUOUS_FACTS")
        self.assertIn("direction", ambiguous["details"]["distinguishing"])
        nothing = run(SONNAR, {"id": "m", "fact": "performance.mtf", "op": "gt", "value": 0.9,
                               "select": {**select, "frequency_lp_mm": 40}})
        self.assertEqual(nothing["reason"], "CONDITIONS_DIFFER")
        self.assertEqual(nothing["details"]["select"]["frequency_lp_mm"]["available"], [10, 30])

    def test_source_ops_default_to_source_and_methods(self):
        # has_source / has_sourced_method named without a member check `source` / `methods`, not
        # the value member (a resolving source must never be reported EVIDENCE_MISSING).
        view = as_view(P2B)
        for fact, op, path in (("performance.relative_illumination", "has_source", "source"),
                               ("cost_proxy", "has_source", "source"),
                               ("analysis_evidence.ghost", "has_sourced_method", "methods"),
                               ("performance.relative_illumination.source", "has_source", "source")):
            with self.subTest(fact=fact):
                expanded = ev.expand_constraint({"fact": fact, "op": op}, view, 0)
                self.assertEqual(expanded["predicates"][0]["path"], path)
                self.assertEqual(run(P2B, {"fact": fact, "op": op})["outcome"], "MATCH")
        # An explicit path still wins; a fact without a source stays UNCONFIRMED at `source`.
        explicit = ev.expand_constraint({"fact": "cost_proxy", "op": "has_source", "path": "basis"}, view, 0)
        self.assertEqual(explicit["predicates"][0]["path"], "basis")
        unsourced = run(P2B, {"fact": "first_order.effective_focal_length_mm", "op": "has_source"}, detail=True)
        self.assertEqual((unsourced["outcome"], unsourced["reason"]), ("UNCONFIRMED", "VALUE_MISSING"))
        self.assertEqual(unsourced["predicates"][0]["path"], "source")

    def test_value_member_missing(self):
        result = run(P2B, {"id": "m", "fact": "performance.mtf", "op": "gt", "value": 0.7,
                           "select": {"field_fraction": 0.8}})
        self.assertEqual(result["reason"], "VALUE_MISSING")
        self.assertIn("tangential", result["details"]["numeric_members"])
        sagittal = run(P2B, {"id": "m", "fact": "performance.mtf.sagittal", "op": "gt", "value": 0.7,
                             "select": {"field_fraction": 0.8}})
        self.assertEqual(sagittal["outcome"], "MATCH")


class Semantics(unittest.TestCase):
    def test_json_equality(self):
        self.assertTrue(ev.json_equal(0, 0.0))
        self.assertTrue(ev.json_equal([1, {"a": 2.0}], [1.0, {"a": 2}]))
        self.assertFalse(ev.json_equal(True, 1))
        self.assertFalse(ev.json_equal(False, 0))
        self.assertFalse(ev.json_equal([1, 2], [2, 1]))
        self.assertFalse(ev.json_equal({"a": 1}, {"a": 1, "b": None}))
        self.assertFalse(ev.json_equal(float("nan"), float("nan")))
        record = copy.deepcopy(P2B)
        record["image_format"]["width_mm"] = True
        self.assertEqual(run(record, CONSTRAINTS["sensor"])["outcome"], "DOES_NOT_MATCH")

    def test_value_type(self):
        result = run(P2B, {"fact": "design.conjugate", "op": "gte", "value": 1})
        self.assertEqual((result["outcome"], result["reason"]), ("UNCONFIRMED", "VALUE_TYPE"))

    def test_between_shortfall_names_violated_bound(self):
        low = run(P2B, {"fact": "first_order.effective_focal_length_mm", "op": "between_inclusive",
                        "value": [19, 20]})
        self.assertEqual(low["shortfalls"][0]["limit"], 19)
        high = run(P2B, {"fact": "first_order.effective_focal_length_mm", "op": "between_inclusive",
                         "value": [10, 18]})
        self.assertEqual(high["shortfalls"][0]["limit"], 18)

    def test_evidence_predicates(self):
        record = copy.deepcopy(P2B)
        for method in record["analysis_evidence"]["ghost"]["methods"]:
            method["source"]["sha256"] = "0" * 64
        result = run(record, CONSTRAINTS["ghost_evidence"])
        self.assertEqual(result["reason"], "EVIDENCE_UNRESOLVED")
        self.assertEqual(result["details"]["statuses"], ["HASH_MISMATCH"])
        record["analysis_evidence"]["tolerance"]["mtf_gate"]["source"]["section"] = "nope"
        self.assertEqual(run(record, CONSTRAINTS["tolerance_evidence"])["reason"], "EVIDENCE_UNRESOLVED")

    def test_configuration_outcome_and_rank(self):
        constraints = [CONSTRAINTS["efl"], {"fact": "image_format.width_mm", "op": "eq", "value": 35},
                       {"fact": "aperture.f_number", "op": "lte", "value": 2}]
        result = ev.evaluate_configuration(as_view(P2B), constraints, CTX)
        self.assertEqual(result["outcome"], "DOES_NOT_MATCH")
        self.assertEqual(result["counts"], {"MATCH": 1, "UNCONFIRMED": 1, "DOES_NOT_MATCH": 1})
        self.assertEqual([c["id"] for c in result["constraints"]], ["efl", "c1", "c2"])
        self.assertNotIn("fact", result["constraints"][0])
        empty = ev.evaluate_configuration(as_view(P2B), [], CTX)
        self.assertEqual(empty["outcome"], "MATCH")
        self.assertLess(ev.configuration_rank(empty, 1), ev.configuration_rank(result, 0))


class Validation(unittest.TestCase):
    def assertRequestError(self, raw, code, pointer):
        with self.assertRaises(ev.RequestError) as raised:
            ev.validate_constraint(raw, "/constraints/0")
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(raised.exception.details["pointer"], pointer)
        return raised.exception

    def test_fixture_constraints_validate(self):
        for constraint in CASE["constraints"]:
            ev.validate_constraint(constraint, "/constraints/0")

    def test_closed_schema(self):
        error = self.assertRequestError({"fact": "first_order.total_track_mm", "op": "lte", "value": 1,
                                         "condition": {}}, "INVALID_REQUEST", "/constraints/0/condition")
        self.assertIn("conditions", error.details["did_you_mean"])
        self.assertRequestError({"scope": ["design"], "predicates": [{"path": "x", "op": "eq", "value": 1,
                                                                      "vale": 2}]},
                                "INVALID_REQUEST", "/constraints/0/predicates/0/vale")

    def test_unsupported_operator(self):
        error = self.assertRequestError({"scope": ["design"], "predicates": [{"path": "x", "op": "gtee", "value": 1}]},
                                        "UNSUPPORTED_OPERATOR", "/constraints/0/predicates/0/op")
        self.assertIn("gte", error.details["did_you_mean"])

    def test_bad_shapes(self):
        self.assertRequestError({"fact": "a.b", "op": "between_inclusive", "value": [3, 1]},
                                "INVALID_REQUEST", "/constraints/0/value")
        self.assertRequestError({"fact": "a.b", "op": "lte", "value": "x"}, "INVALID_REQUEST", "/constraints/0/value")
        self.assertRequestError({"fact": "a.b", "op": "eq", "value": 1, "select": {"x": 1}},
                                "INVALID_REQUEST", "/constraints/0/select")
        self.assertRequestError({"fact": "a.b", "op": "eq", "value": 1, "tolerance": 1},
                                "INVALID_REQUEST", "/constraints/0/tolerance")
        self.assertRequestError({"op": "eq"}, "INVALID_REQUEST", "/constraints/0")
        self.assertRequestError({"scope": ["a"], "predicates": []}, "INVALID_REQUEST", "/constraints/0/predicates")

    def test_integer_beyond_double_range_is_invalid_not_a_crash(self):
        huge = 10 ** 400  # valid JSON, but math.isfinite(huge) raises OverflowError
        self.assertFalse(ev._is_number(huge))
        self.assertFalse(ev._is_number(-huge))
        self.assertTrue(ev._is_number(10 ** 300))
        for op in ("lte", "gt"):
            self.assertRequestError({"fact": "a.b", "op": op, "value": huge}, "INVALID_REQUEST",
                                    "/constraints/0/value")
        self.assertRequestError({"fact": "a.b", "op": "between_inclusive", "value": [0, huge]},
                                "INVALID_REQUEST", "/constraints/0/value")
        with self.assertRaises(ev.RequestError) as raised:
            ev.validate_target({"fact": "a.b", "target": 20, "tolerance": huge}, "/targets/0")
        self.assertEqual(raised.exception.details["pointer"], "/targets/0/tolerance")


class TargetsAndPreferences(unittest.TestCase):
    def test_target_value_and_deviation(self):
        value, reason, info = ev.target_value(as_view(P2B), {"fact": "first_order.effective_focal_length_mm",
                                                             "target": 24}, CTX)
        self.assertIsNone(reason)
        self.assertEqual(value, P2B["first_order"]["effective_focal_length_mm"]["value"])
        self.assertEqual(info["unit"], "mm")
        deviation = ev.target_deviation(27.0, {"target": 24, "tolerance": 3})
        self.assertEqual(deviation["normalized"], 1.0)
        self.assertEqual(ev.target_deviation(27.0, {"target": 24, "relative_tolerance": 0.25})["normalized"], 0.5)
        self.assertEqual(ev.target_deviation(27.0, {"target": 24})["normalized"], 0.125)
        zero = ev.target_deviation(-2.0, {"target": 0})
        self.assertEqual((zero["normalized"], zero["scale_basis"]), (2.0, "absolute"))
        with self.assertRaises(ev.RequestError):
            ev.target_deviation(1.0, {"target": 1, "tolerance": 0})

    def test_tiny_target_scale_is_a_request_error(self):
        # A scale that is not a normal positive double, or a quotient that overflows, is
        # INVALID_REQUEST with a pointer, never an infinite ranking value.
        for raw, pointer in (({"fact": "a.b", "target": 24, "tolerance": 1e-310}, "/targets/0/tolerance"),
                             ({"fact": "a.b", "target": 1e-320}, "/targets/0/target"),
                             ({"fact": "a.b", "target": 1e-300, "relative_tolerance": 1e-10},
                              "/targets/0/relative_tolerance")):
            with self.subTest(raw=raw):
                with self.assertRaises(ev.RequestError) as raised:
                    ev.validate_target(raw, "/targets/0")
                self.assertEqual(raised.exception.code, "INVALID_REQUEST")
                self.assertEqual(raised.exception.details["pointer"], pointer)
        with self.assertRaises(ev.RequestError) as raised:
            ev.target_deviation(1e300, {"fact": "a.b", "target": -1e300, "tolerance": 1e-10}, pointer="/targets/0")
        self.assertEqual(raised.exception.details["pointer"], "/targets/0")
        smallest = ev.target_deviation(25.0, {"target": 24, "tolerance": sys.float_info.min})
        self.assertTrue(math.isfinite(smallest["normalized"]))
        ev.validate_target({"fact": "a.b", "target": 24, "tolerance": sys.float_info.min}, "/targets/0")

    def test_target_unknowns(self):
        view = as_view(P2B)
        self.assertEqual(ev.target_value(view, {"fact": "aperture.f_number", "target": 2}, CTX)[1], "FACT_MISSING")
        self.assertEqual(ev.target_value(view, {"fact": "spectrum.sampling_span_nm", "path": "min_nm",
                                                "target": 450}, CTX)[1], "DISCOVERY_ONLY")
        self.assertEqual(ev.target_value(as_view(SONNAR), {"fact": "performance.mtf", "target": 1}, CTX)[1],
                         "AMBIGUOUS_FACTS")
        with self.assertRaises(ev.RequestError):
            ev.target_value(view, {"fact": "spectrum.sampling_span_nm", "target": 500}, CTX)
        with self.assertRaises(ev.RequestError):
            ev.target_value(view, {"fact": "performance.distortion_range", "target": 0}, CTX)
        # Eligibility only: an eligible record whose stop cannot support a match still ranks.
        value, reason, _ = ev.target_value(as_view(load_record("lenslibrary-1792917")),
                                           {"fact": "aperture.working_f_number", "target": 4}, CTX)
        self.assertEqual((value, reason), (4.511507, None))
        self.assertEqual(ev.target_value(as_view(load_record("lenslibrary-7821720c")),
                                         {"fact": "aperture.working_f_number", "target": 4}, CTX)[1],
                         "STOP_NOT_ELIGIBLE")

    def test_fixture_preference(self):
        preference = CASE["ranking_preference"]
        value, reason, _ = ev.preference_value(as_view(P2B), preference, CTX)
        self.assertEqual((value, reason), (30, None))
        self.assertEqual(ev.preference_rank_entry(value, "ascending"), [0, 30])
        self.assertEqual(ev.preference_rank_entry(value, "descending"), [0, -30])
        self.assertEqual(ev.preference_rank_entry(None, "ascending"), [1, None])
        other = dict(preference, aspheric_element_weight=2)
        value, reason, info = ev.preference_value(as_view(P2B), other, CTX)
        self.assertEqual((value, reason), (None, "PREFERENCE_NOT_COMPARABLE"))
        self.assertEqual(info["details"]["differs"]["aspheric_element_weight"], {"expected": 2, "actual": 3})
        self.assertEqual(ev.preference_value(as_view(SONNAR), preference, CTX)[1], "FACT_MISSING")

    def test_fixture_preference_is_guarded_like_shorthand(self):
        # Amendment 10.9: both forms obtain the ranking value through the same guards.
        for record in (P2B, SONNAR):
            view = as_view(record)
            fixture = ev.preference_value(view, {"scope": ["spectrum", "sampling_span_nm"], "metric": "max_nm",
                                                 "direction": "descending"}, CTX)
            shorthand = ev.preference_value(view, {"fact": "spectrum.sampling_span_nm.max_nm",
                                                   "direction": "descending"}, CTX)
            self.assertEqual(fixture[:2], (None, "DISCOVERY_ONLY"))
            self.assertEqual(shorthand[:2], (None, "DISCOVERY_ONLY"))
            self.assertEqual(fixture[2]["details"]["eligible_for"], "DISCOVERY_ONLY")
        coverage = ev.preference_value(as_view(P2B), {"scope": ["spectrum", "continuous_coverage_nm"],
                                                      "metric": "max_nm", "direction": "descending"}, CTX)
        self.assertEqual(coverage[:2], (None, "COVERAGE_UNCONFIRMED"))
        # The cost form itself still ranks.
        self.assertEqual(ev.preference_value(as_view(P2B), CASE["ranking_preference"], CTX)[:2], (30, None))

    def test_shorthand_preference(self):
        value, reason, _ = ev.preference_value(
            as_view(P2B), {"fact": "cost_proxy.weighted_spherical_element_count", "direction": "ascending"}, CTX)
        self.assertEqual((value, reason), (30, None))
        ev.validate_preferences([CASE["ranking_preference"],
                                 {"fact": "first_order.total_track_mm", "direction": "descending"}], "/preferences")
        with self.assertRaises(ev.RequestError) as raised:
            ev.validate_preferences(CASE["ranking_preference"], "/preferences")
        self.assertIn("list", raised.exception.hint)


if __name__ == "__main__":
    unittest.main()

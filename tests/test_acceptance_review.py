"""Regressions for the PR #40 acceptance review: matching semantics, provenance checks, finite
arithmetic, sampling grids and configuration-scoped evidence.

Every user-visible failure is reproduced through the public CLI (`python -m os_optics_lens` as a
subprocess) against the checkout index or a synthetic corpus; focused unit tests cover the checks
underneath.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
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

from os_optics_lens import evaluate as ev  # noqa: E402
from os_optics_lens import output, query  # noqa: E402
from os_optics_lens.build import build_index  # noqa: E402
from os_optics_lens.errors import LensError  # noqa: E402
from os_optics_lens.store import open_index  # noqa: E402
from os_optics_lens.vocabulary import load_vocabulary  # noqa: E402

_spec = importlib.util.spec_from_file_location("_review_build_helpers", ROOT / "tests/test_build_foundation.py")
helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(helpers)

P2B = "yutong-2025-mirrorless-18-p2b"
DUMP_RECORD = "lenslibrary-528155"
P2B_RECORD = json.loads((ROOT / "records/yutong-cup" / P2B / "record.json").read_text(encoding="utf-8"))
ALL = ["MATCH", "UNCONFIRMED", "DOES_NOT_MATCH"]
EFL = "first_order.effective_focal_length_mm"
MTF = "performance.mtf_field_minimum"
WAVELENGTHS = [486, 587, 656]
GRID_P2B = next(e for e in P2B_RECORD["performance"] if e["metric"] == "mtf_field_minimum")["sampled_field_fractions"]

_TMP: tempfile.TemporaryDirectory | None = None
TMP = Path()
INDEX = Path()


def setUpModule() -> None:
    global _TMP, TMP, INDEX
    _TMP = tempfile.TemporaryDirectory(prefix="os-optics-lens-review-")
    TMP = Path(_TMP.name)
    INDEX = TMP / "checkout.sqlite"
    build_index(ROOT, INDEX)


def tearDownModule() -> None:
    if _TMP is not None:
        _TMP.cleanup()


def run_cli(index: Path, *args: str, request: Any = None) -> tuple[int, dict[str, Any]]:
    """One CLI invocation; `request` is passed on stdin to `search -`."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("OS_OPTICS_LENS_INDEX", "OS_OPTICS_LENS_DEBUG", "PYTHONIOENCODING", "PYTHONUTF8")}
    env["PYTHONPATH"] = str(SRC)
    argv = [sys.executable, "-m", "os_optics_lens", "--index", str(index), *args]
    data = b""
    if request is not None:
        argv.append("-")
        data = json.dumps(request).encode("utf-8")
    proc = subprocess.run(argv, input=data, capture_output=True, env=env, cwd=str(TMP), timeout=300)
    assert proc.stderr == b"", proc.stderr
    return proc.returncode, json.loads(proc.stdout.decode("ascii"))


class CliTestCase(unittest.TestCase):
    index: Path

    def cli_ok(self, *args: str, request: Any = None, index: Path | None = None) -> dict[str, Any]:
        code, doc = run_cli(index or self.index_path(), *args, request=request)
        self.assertEqual(code, 0, doc)
        self.assertTrue(doc["ok"], doc)
        return doc

    def cli_error(self, error: str, exit_code: int, *args: str, request: Any = None,
                  index: Path | None = None) -> dict[str, Any]:
        code, doc = run_cli(index or self.index_path(), *args, request=request)
        self.assertFalse(doc["ok"], doc)
        self.assertEqual((doc["error"]["code"], code), (error, exit_code), doc)
        return doc["error"]

    def index_path(self) -> Path:
        return INDEX

    def candidate(self, record_id: str, constraint: dict[str, Any] | None = None, *, mode: str = "specification",
                  index: Path | None = None, **extra: Any) -> dict[str, Any]:
        request = {"mode": mode, "filters": {"record_ids": [record_id]}, "include": ALL, "detail": "full", **extra}
        if constraint is not None:
            request["constraints"] = [constraint]
        doc = self.cli_ok("search", request=request, index=index)
        [candidate] = doc["result"]["candidates"]
        return candidate


# F1 requested condition members must exist ------------------------------------------------------


def mtf_constraint(spectrum: dict[str, Any]) -> dict[str, Any]:
    return {"id": "mtf", "fact": MTF, "op": "gte", "value": 0.7, "conditions": {"spectrum": spectrum}}


class ConditionPresenceCli(CliTestCase):
    def test_unstated_spectrum_member_is_unconfirmed(self) -> None:
        spectrum = {"sampling_wavelengths_nm": WAVELENGTHS, "incident_weights": [2, 2, 2],
                    "wavelength_medium": "air"}
        result = self.candidate(P2B, mtf_constraint(spectrum))["constraints"]["mtf"]
        self.assertEqual((result["outcome"], result["reason"]), ("UNCONFIRMED", "CONDITIONS_DIFFER"))
        self.assertEqual(result["details"]["differs"],
                         {"spectrum.wavelength_medium": {"expected": "air", "present": False}})

    def test_proportional_weights_still_match(self) -> None:
        spectrum = {"sampling_wavelengths_nm": WAVELENGTHS, "incident_weights": [2, 2, 2]}
        self.assertEqual(self.candidate(P2B, mtf_constraint(spectrum))["constraints"]["mtf"]["outcome"], "MATCH")

    def test_absent_member_never_equals_requested_null(self) -> None:
        constraint = {"id": "cra", "fact": "performance.cra", "op": "lte", "value": 18,
                      "conditions": {"angle_reference": "image_plane_normal", "direction": None}}
        result = self.candidate(P2B, constraint)["constraints"]["cra"]
        self.assertEqual((result["outcome"], result["reason"]), ("UNCONFIRMED", "CONDITIONS_DIFFER"))
        self.assertEqual(result["details"]["differs"], {"direction": {"expected": None, "present": False}})

    def test_partly_pinned_spectrum_reports_unpinned_members(self) -> None:
        constraint = mtf_constraint({"sampling_wavelengths_nm": WAVELENGTHS})
        doc = self.cli_ok("search", request={"mode": "specification", "filters": {"record_ids": [P2B]},
                                             "constraints": [constraint]})
        [candidate] = doc["result"]["candidates"]
        self.assertIn("spectrum.incident_weights", candidate["constraints"]["mtf"]["unpinned"])
        self.assertNotIn("spectrum.sampling_wavelengths_nm", candidate["constraints"]["mtf"]["unpinned"])

    def test_weights_without_wavelengths_are_invalid_everywhere(self) -> None:
        conditions = {"spectrum": {"incident_weights": [1, 1, 1]}}
        requests = (
            {"mode": "specification", "constraints": [dict(mtf_constraint(conditions["spectrum"]))]},
            {"mode": "specification", "constraints": [{
                "scope": ["performance"], "metric": "mtf_field_minimum", "conditions": conditions,
                "predicates": [{"path": "value", "op": "gt", "value": 0}]}]},
            {"mode": "starting_point", "targets": [{"fact": MTF, "target": 0.7, "conditions": conditions}]},
            {"mode": "starting_point", "preferences": [{"fact": MTF, "direction": "descending",
                                                        "conditions": conditions}]},
        )
        for request in requests:
            with self.subTest(request=request):
                error = self.cli_error("INVALID_REQUEST", 2, "search", request=request)
                self.assertTrue(error["details"]["pointer"].endswith("/conditions/spectrum"), error)

    def test_required_sampled_field_fractions_must_be_a_non_empty_number_list(self) -> None:
        for bad in (None, [], "0,0.4", [0, None], [True]):
            for key, item in (
                    ("constraints", {"fact": MTF, "op": "gt", "value": 0.5}),
                    ("targets", {"fact": MTF, "target": 0.7}),
                    ("preferences", {"fact": MTF, "direction": "descending"})):
                with self.subTest(bad=bad, key=key):
                    request = {"mode": "starting_point", key: [{**item, "required_sampled_field_fractions": bad}]}
                    error = self.cli_error("INVALID_REQUEST", 2, "search", request=request)
                    self.assertTrue(error["details"]["pointer"].endswith("/required_sampled_field_fractions"))


class ConditionPresenceUnit(unittest.TestCase):
    CTX = ev.EvalContext()

    def view(self, conditions: dict[str, Any], **entry: Any) -> dict[str, Any]:
        return {"performance": [{"metric": "m", "value": 1.0, "conditions": conditions, **entry}],
                "cost_proxy": {"count": 3, "scope": "all", "spherical_element_weight": None}}

    def outcome(self, stored: dict[str, Any], requested: dict[str, Any]) -> str:
        constraint = {"scope": ["performance"], "metric": "m", "conditions": requested,
                      "predicates": [{"path": "value", "op": "gt", "value": 0}]}
        return ev.evaluate_constraint(self.view(stored), constraint, self.CTX)["outcome"]

    def test_presence_and_null(self) -> None:
        self.assertEqual(self.outcome({"direction": None}, {"direction": None}), "MATCH")
        self.assertEqual(self.outcome({}, {"direction": None}), "UNCONFIRMED")
        self.assertEqual(self.outcome({"direction": "x"}, {"direction": None}), "UNCONFIRMED")

    def test_spectrum_member_by_member(self) -> None:
        stored = {"spectrum": {"sampling_wavelengths_nm": [1, 2], "incident_weights": [1, 3], "medium": None}}
        wavelengths = {"sampling_wavelengths_nm": [1, 2.0]}
        for requested, expected in (
                (wavelengths, "MATCH"),
                ({**wavelengths, "incident_weights": [2, 6]}, "MATCH"),
                ({**wavelengths, "incident_weights": [6, 2]}, "UNCONFIRMED"),
                ({**wavelengths, "medium": None}, "MATCH"),
                ({**wavelengths, "medium": "air"}, "UNCONFIRMED"),
                ({**wavelengths, "temperature_c": None}, "UNCONFIRMED"),
                ({"sampling_wavelengths_nm": [2, 1]}, "UNCONFIRMED")):
            with self.subTest(requested=requested):
                self.assertEqual(self.outcome(stored, {"spectrum": requested}), expected)

    def test_select_target_and_preference_conditions(self) -> None:
        view = self.view({"a": 1})
        constraint = {"scope": ["performance"], "metric": "m", "select": {"direction": None},
                      "predicates": [{"path": "value", "op": "gt", "value": 0}]}
        result = ev.evaluate_constraint(view, constraint, self.CTX)
        self.assertEqual(result["reason"], "CONDITIONS_DIFFER")
        self.assertEqual(result["details"]["select"]["direction"],
                         {"expected": None, "available": [], "present": False})
        spec = {"fact": "performance.m", "conditions": {"direction": None}}
        for value, reason, _ in (ev.target_value(view, {**spec, "target": 1}, self.CTX),
                                 ev.preference_value(view, {**spec, "direction": "ascending"}, self.CTX)):
            self.assertEqual((value, reason), (None, "CONDITIONS_DIFFER"))

    def test_fixture_preference_pins_need_presence(self) -> None:
        preference = {"scope": ["cost_proxy"], "metric": "count", "direction": "ascending", "scope_of_cost": "all"}
        view = self.view({})
        value, reason, info = ev.preference_value(
            view, {**preference, "aspheric_element_weight": None}, self.CTX)
        self.assertEqual((value, reason), (None, "PREFERENCE_NOT_COMPARABLE"))
        self.assertEqual(info["details"]["differs"], {"aspheric_element_weight": {"expected": None, "present": False}})
        self.assertEqual(ev.preference_value(view, {**preference, "spherical_element_weight": None}, self.CTX)[:2],
                         (3, None))


# F2 provenance checks are never matched or ranked ------------------------------------------------

CHECK = "field.prescription_dump_check"


class ProvenanceCheckCli(CliTestCase):
    def test_constraints_reaching_the_check_are_comparison_only(self) -> None:
        constraints = (
            {"fact": CHECK, "op": "eq", "value": "MATCHES"},
            {"scope": ["field"], "predicates": [{"path": "prescription_dump_check", "op": "eq", "value": "MATCHES"}]},
            {"scope": ["field", "prescription_dump_check"], "predicates": [{"path": "", "op": "eq", "value": "x"}]},
            {"fact": "materials.catalog_comparisons", "op": "eq", "value": []},
        )
        for constraint in constraints:
            with self.subTest(constraint=constraint):
                result = self.candidate(DUMP_RECORD, {"id": "c", **constraint})["constraints"]["c"]
                self.assertEqual((result["outcome"], result.get("reason")), ("UNCONFIRMED", "COMPARISON_ONLY"), result)

    def test_raw_field_and_glass_data_are_unavailable_to_constraints(self) -> None:
        for record_id, key in ((DUMP_RECORD, "field"), ("lenslibrary-4037934a", "materials.named_glasses")):
            with self.subTest(record_id=record_id, key=key):
                constraint = {"id": "c", "fact": key, "path": "", "op": "ne", "value": 1}
                result = self.candidate(record_id, constraint)["constraints"]["c"]
                self.assertEqual((result["outcome"], result.get("reason")), ("UNCONFIRMED", "FACT_MISSING"), result)

    def test_naming_the_check_is_an_error_with_a_provenance_hint(self) -> None:
        requests = (
            {"mode": "starting_point", "targets": [{"fact": CHECK, "target": 1}]},
            {"mode": "starting_point", "targets": [{"fact": "field.half_field_deg", "target": 1,
                                                    "path": "prescription_dump_check"}]},
            {"mode": "starting_point", "preferences": [{"fact": CHECK, "direction": "ascending"}]},
            {"mode": "starting_point", "preferences": [{"scope": ["field"], "metric": "prescription_dump_check",
                                                        "direction": "ascending"}]},
            {"mode": "starting_point", "filters": {"ranges": [{"fact": CHECK, "min": 0}]}},
            {"mode": "starting_point", "fields": [CHECK]},
        )
        errors = [self.cli_error("UNKNOWN_FIELD", 2, "search", request=request) for request in requests]
        errors.append(self.cli_error("UNKNOWN_FIELD", 2, "fields", "--fact", CHECK))
        errors.append(self.cli_error("UNKNOWN_FIELD", 2, "facets", "--range", f"{CHECK}=0:1"))
        errors.append(self.cli_error("UNKNOWN_FIELD", 2, "compare", DUMP_RECORD, "--fields", CHECK))
        errors.append(self.cli_error("UNKNOWN_FIELD", 2, "inspect", DUMP_RECORD, "--fields", CHECK))
        errors.append(self.cli_error("UNKNOWN_FIELD", 2, "inspect", DUMP_RECORD, "--fields",
                                     "first_order.engine_check"))
        for error in errors:
            self.assertIn("provenance check that cannot be matched or ranked", error["hint"], error)
            self.assertIn("inspect <record_id> --provenance", error["hint"])
            self.assertIn("provenance_check", error["details"])

    def test_intake_checks_are_omitted_from_inspection(self) -> None:
        listed = self.cli_ok("fields", "--prefix", "field.")["result"]["fields"]
        self.assertNotIn(CHECK, listed)
        inspected = self.cli_ok("inspect", DUMP_RECORD, "--fields", "first_order", "--sections", "first_order",
                                "--provenance")["result"]
        self.assertNotIn(CHECK, inspected["fact_keys"])
        self.assertNotIn("engine_check", inspected["sections"]["first_order"])
        self.assertTrue(inspected["fields"])
        for item in inspected["fields"].values():
            self.assertNotIn("checks", item)
        self.assertNotIn("source_prescription_dump", inspected["provenance"])


class ProvenanceCheckUnit(unittest.TestCase):
    def test_evaluator_and_vocabulary_agree_on_check_members(self) -> None:
        self.assertEqual(set(load_vocabulary().fact_extraction["check_members"]), set(ev._NOT_FACTS))

    def test_fixture_preference_and_select_are_guarded(self) -> None:
        view = {"field": {"prescription_dump_check": "MATCHES"},
                "performance": [{"metric": "m", "value": 1, "engine_check": 1}]}
        preference = {"scope": ["field"], "metric": "prescription_dump_check", "direction": "ascending"}
        self.assertEqual(ev.preference_value(view, preference, ev.EvalContext())[1], "COMPARISON_ONLY")
        constraint = {"scope": ["performance"], "metric": "m", "select": {"engine_check": 1},
                      "predicates": [{"path": "value", "op": "gt", "value": 0}]}
        self.assertEqual(ev.evaluate_constraint(view, constraint, ev.EvalContext())["reason"], "COMPARISON_ONLY")


# F3 finite inputs stay serializable --------------------------------------------------------------

HUGE = 1.7e308
EXTREMES = (HUGE, -HUGE, 5e-324, -5e-324, 1e-308, 0, 10 ** 308, -(10 ** 308), 1)


def extreme_values(record: dict[str, Any]) -> None:
    record["first_order"]["effective_focal_length_mm"]["value"] = 10 ** 308
    record["first_order"]["back_focal_length_mm"] = {"value": -HUGE, "basis": "ENGINE_COMPUTED"}
    record["first_order"]["total_track_mm"] = {"value": 5e-324, "basis": "PROJECT_DERIVED"}
    record["spectrum"]["continuous_coverage_nm"] = {
        "state": "CONFIRMED", "min_nm": -(10 ** 308), "max_nm": HUGE, "basis": "ENGINE_COMPUTED",
    }


class FiniteArithmeticCli(CliTestCase):
    def test_tiny_limit_reports_the_gap_without_a_relative_value(self) -> None:
        doc = self.cli_ok("search", request={
            "mode": "specification", "filters": {"record_ids": [P2B]}, "include": ["DOES_NOT_MATCH"],
            "constraints": [{"id": "efl", "fact": EFL, "op": "lt", "value": 1e-308}]})
        [candidate] = doc["result"]["candidates"]
        self.assertEqual(candidate["outcome"], "DOES_NOT_MATCH")
        [shortfall] = candidate["shortfalls"]
        self.assertAlmostEqual(shortfall["shortfall"], 18.726, places=3)
        self.assertIsNone(shortfall["relative"])

    def test_quick_flag_form(self) -> None:
        doc = self.cli_ok("search", "--mode", "specification", "--record-id", P2B,
                          "--where", f"{EFL} lt 5e-324", "--include", "DOES_NOT_MATCH")
        self.assertIsNone(doc["result"]["candidates"][0]["shortfalls"][0]["relative"])

    def test_overflowing_target_inputs_are_request_errors(self) -> None:
        for target in ({"fact": EFL, "target": 10 ** 200, "relative_tolerance": 10 ** 200},
                       {"fact": EFL, "target": HUGE, "tolerance": 5e-324},
                       {"fact": EFL, "target": HUGE, "tolerance": 1e-300},
                       {"fact": EFL, "target": 1, "tolerance": 1, "weight": HUGE}):
            with self.subTest(target=target):
                self.cli_error("INVALID_REQUEST", 2, "search",
                               request={"mode": "starting_point", "filters": {"record_ids": [P2B]},
                                        "targets": [target]})


class FiniteArithmeticSweep(unittest.TestCase):
    """Every numeric operator, target and preference over extreme stored and requested values:
    each call answers or raises a LensError, and the answer serializes."""

    def test_extremes_never_reach_internal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "corpus"
            helpers.SynthCorpus(root).add("extreme", mutate=extreme_values)
            build_index(root, Path(tmp) / "index.sqlite")
            requests = []
            facts = (EFL, "first_order.back_focal_length_mm", "first_order.total_track_mm", "field.half_field_deg")
            for fact in facts:
                for x in EXTREMES:
                    for op in ("gt", "gte", "lt", "lte", "eq"):
                        requests.append({"mode": "specification", "constraints": [{"fact": fact, "op": op, "value": x}]})
                    for target in (
                            {"fact": fact, "target": x},
                            {"fact": fact, "target": x, "tolerance": HUGE},
                            {"fact": fact, "target": x, "relative_tolerance": 10 ** 308},
                            {"fact": fact, "target": x, "tolerance": 1e-300, "weight": 10 ** 308}):
                        requests.append({"mode": "starting_point", "targets": [target]})
                    requests.append({"mode": "starting_point", "targets": [{"fact": fact, "target": 1, "weight": HUGE},
                                                                           {"fact": EFL, "target": x, "weight": HUGE}],
                                     "preferences": [{"fact": fact, "direction": "descending"}]})
                for low, high in ((-HUGE, HUGE), (HUGE, HUGE), (-HUGE, -HUGE), (0, 5e-324),
                                  (-(10 ** 308), 10 ** 308)):
                    for constraint in (
                            {"fact": fact, "op": "between_inclusive", "value": [low, high]},
                            {"fact": "spectrum.continuous_coverage_nm", "op": "covers_interval", "value": [low, high]}):
                        requests.append({"mode": "specification", "constraints": [constraint]})
            for x in EXTREMES:
                requests.append({"mode": "specification", "constraints": [
                    {"fact": "spectrum.sampling_wavelengths_nm", "op": "includes_all", "value": [x, -HUGE],
                     "tolerance": HUGE}]})
                requests.append({"mode": "specification", "constraints": [
                    {"fact": "spectrum.sampling_wavelengths_nm", "op": "proportional_weights",
                     "value": [HUGE, 5e-324, abs(x) or 1]}]})
            answered = 0
            with open_index(Path(tmp) / "index.sqlite") as index:
                for request in requests:
                    request["include"] = ALL
                    for detail in ("compact", "full"):
                        try:
                            output.serialize(query.search(index, {**request, "detail": detail}))
                            answered += 1
                        except LensError as error:
                            output.serialize(output.error_response(error))
                            self.assertNotEqual(error.code, "INTERNAL", (request, error.to_dict()))
                        except Exception as error:  # pragma: no cover - the regression
                            self.fail(f"{type(error).__name__}: {error} for {request}")
                output.serialize(query.compare(index, ["extreme"], fields=list(facts)))
                output.serialize(query.fields(index, fact=EFL))
            self.assertGreater(answered, len(requests))


# F4 the sampling grid is a comparison condition --------------------------------------------------

SWEEPS = {"sweep3": [0, 0.4, 0.8], "sweep5": [0, 0.2, 0.4, 0.6, 0.8], "nogrid": None}


def sweep(grid: list[float] | None):
    def mutate(record: dict[str, Any]) -> None:
        entry = {"metric": "mtf_field_minimum", "basis": "ENGINE_COMPUTED", "value": 0.7,
                 "conditions": {"frequency_lp_mm": 40, "field_start": 0, "field_end": 0.8}}
        if grid is not None:
            entry["sampled_field_fractions"] = grid
        record["performance"] = [entry]
        record["design"]["aperture_validity"]["satisfies_stop_dependent_constraints"] = True
    return mutate


class SamplingGridCli(CliTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name) / "corpus"
        corpus = helpers.SynthCorpus(root)
        for record_id, grid in SWEEPS.items():
            corpus.add(record_id, with_evidence=False, mutate=sweep(grid))
        corpus.add("sweep3b", with_evidence=False, mutate=sweep([0.0, 0.4, 0.8]))
        cls.sweeps = Path(cls._tmp.name) / "index.sqlite"
        build_index(root, cls.sweeps)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def index_path(self) -> Path:
        return self.sweeps

    def row(self, *refs: str) -> dict[str, Any]:
        [row] = self.cli_ok("compare", *refs, "--fields", MTF)["result"]["rows"]
        return row

    def test_compare_distinguishes_grids(self) -> None:
        for refs in (("sweep3", "sweep5"), ("sweep3", "nogrid")):
            with self.subTest(refs=refs):
                row = self.row(*refs)
                self.assertFalse(row["comparable"])
                self.assertEqual(row["differs_by"], ["sampled_field_fractions"])
                self.assertEqual([cell["sampled_field_fractions"] for cell in row["values"]],
                                 [SWEEPS[ref] for ref in refs])
        same = self.row("sweep3", "sweep3b")
        self.assertTrue(same["comparable"], same)
        self.assertNotIn("sampled_field_fractions", same["values"][0])

    def test_fields_variants_and_example(self) -> None:
        described = self.cli_ok("fields", "--fact", MTF)["result"]
        self.assertEqual(described["condition_variants"], 3)
        grids = [variant.get("sampled_field_fractions") for variant in described["variants"]]
        self.assertCountEqual(grids, SWEEPS.values())
        self.assertEqual(self.cli_ok("fields")["result"]["fields"][MTF]["condition_variants"], 3)
        described = self.cli_ok("fields", "--fact", MTF, "--record-id", "sweep5")["result"]
        example = described["example_constraint"]
        self.assertEqual(example["required_sampled_field_fractions"], SWEEPS["sweep5"])
        self.assertEqual(self.candidate("sweep5", {"id": "e", **example})["constraints"]["e"]["outcome"], "MATCH")
        self.assertEqual(self.candidate("sweep3", {"id": "e", **example})["constraints"]["e"]["reason"],
                         "SAMPLING_DIFFERS")

    def ranked(self, **extra: Any) -> dict[str, dict[str, Any]]:
        doc = self.cli_ok("search", request={"mode": "starting_point", **extra})
        return {c["record_id"]: c for c in doc["result"]["candidates"]}

    def test_mixed_unpinned_grids_are_never_ranked_against_each_other(self) -> None:
        for extra in ({"preferences": [{"fact": MTF, "direction": "descending"}]},
                      {"targets": [{"fact": MTF, "target": 0.7}]}):
            with self.subTest(extra=extra):
                ranked = self.ranked(**extra)
                for record_id in SWEEPS:
                    reasons = [u["reason"] for u in ranked[record_id]["unknowns"]]
                    self.assertEqual(reasons, ["SAMPLING_UNSPECIFIED"], record_id)

    def test_pinned_grid_ranks_only_that_grid(self) -> None:
        pin = {"required_sampled_field_fractions": SWEEPS["sweep5"]}
        for extra in ({"preferences": [{"fact": MTF, "direction": "descending", **pin}]},
                      {"targets": [{"fact": MTF, "target": 0.7, **pin}]}):
            with self.subTest(extra=extra):
                ranked = self.ranked(**extra)
                self.assertEqual(ranked["sweep5"]["unknowns"], [])
                self.assertEqual(ranked["sweep5"]["rank"]["position"], 1)
                for record_id in ("sweep3", "nogrid"):
                    self.assertEqual([u["reason"] for u in ranked[record_id]["unknowns"]], ["SAMPLING_DIFFERS"])

    def test_quick_flags_rank_values_on_an_implicit_grid(self) -> None:
        doc = self.cli_ok("search", "--mode", "starting_point", "--record-id", P2B,
                          "--prefer", f"{MTF}:desc", "--target", f"{MTF}=0.7", index=INDEX)
        [candidate] = doc["result"]["candidates"]
        self.assertEqual(candidate["unknowns"], [])
        self.assertEqual(candidate["rank"]["key"][2], 0)
        self.assertEqual(candidate["rank"]["key"][4][0], 0)

    def test_compact_ranking_unknown_says_how_to_pin_the_grid(self) -> None:
        ranked = self.ranked(preferences=[{"fact": MTF, "direction": "descending"}])
        [unknown] = ranked["sweep3"]["unknowns"]
        self.assertEqual(unknown["reason"], "SAMPLING_UNSPECIFIED")
        self.assertEqual(unknown["details"]["actual"], SWEEPS["sweep3"])
        self.assertIn("required_sampled_field_fractions", unknown["details"]["hint"])

    def test_compact_match_keeps_every_unpinned_mtf_condition_visible(self) -> None:
        constraint = {"id": "m", "fact": MTF, "op": "gt", "value": 0.5}
        request = {"mode": "specification", "filters": {"record_ids": [P2B]}, "constraints": [constraint]}
        [candidate] = self.cli_ok("search", request=request, index=INDEX)["result"]["candidates"]
        unpinned = candidate["constraints"]["m"]["unpinned"]
        self.assertEqual(unpinned["frequency_lp_mm"], 40)
        self.assertNotIn("_more", unpinned)

    def test_unpinned_grid_is_reported_on_a_hard_match(self) -> None:
        constraint = {"id": "m", "fact": MTF, "op": "gt", "value": 0.5}
        result = self.candidate(P2B, constraint, index=INDEX)["constraints"]["m"]
        self.assertEqual(result["outcome"], "MATCH")
        self.assertIn("sampled_field_fractions", result["unpinned"])
        pinned = self.candidate(P2B, {**constraint, "required_sampled_field_fractions": GRID_P2B},
                                index=INDEX)["constraints"]["m"]
        self.assertNotIn("sampled_field_fractions", pinned["unpinned"])


class SamplingGridUnit(unittest.TestCase):
    def test_fixture_preference_resolves_a_sampled_value_with_its_grid(self) -> None:
        view = {"cost_proxy": {"count": 1, "sampled_field_fractions": [0, 1]}}
        preference = {"scope": ["cost_proxy"], "metric": "count", "direction": "ascending"}
        value, reason, info = ev.preference_value(view, preference, ev.EvalContext())
        self.assertEqual((value, reason, info["sampled_field_fractions"]), (1, None, [0, 1]))

    def test_missing_grid_differs_and_says_absent(self) -> None:
        view = {"performance": [{"metric": "m", "value": 1}]}
        constraint = {"fact": "performance.m", "op": "gt", "value": 0, "required_sampled_field_fractions": [0, 1]}
        result = ev.evaluate_constraint(view, constraint, ev.EvalContext())
        self.assertEqual((result["reason"], result["details"]),
                         ("SAMPLING_DIFFERS", {"expected": [0, 1], "present": False}))


# F5 evidence inspection is configuration scoped ---------------------------------------------------


class EvidenceScopeCli(CliTestCase):
    """`zoom`: Wide and Tele each have an `mtf` section in their own file; both reference different
    sections of `shared.json`; the record-level extract is referenced by no source. `claimed`:
    the record-level extract is referenced only by Tele. `phone`: only an evidence_extract."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name) / "corpus"
        corpus = helpers.SynthCorpus(root)
        cls.paths: dict[tuple[str, str], str] = {}

        def evidence(record_id: str, name: str, sections: dict[str, Any]) -> dict[str, str]:
            path = f"records/synth/{record_id}/{name}.json"
            payload = helpers._write_json(root / path, {"schema": "opensci.lens.evidence_extract",
                                                        "record_id": record_id, "sections": sections})
            cls.paths[(record_id, name)] = path
            return {"path": path, "sha256": hashlib.sha256(payload).hexdigest()}

        def zoom_record(record_id: str, extract_claimed_by_tele: bool):
            def mutate(record: dict[str, Any]) -> None:
                wide = evidence(record_id, "wide", {"mtf": {"which": "Wide"}})
                tele = evidence(record_id, "tele", {"mtf": {"which": "Tele"}})
                shared = evidence(record_id, "shared", {"wide_note": 1, "tele_note": 2, "common": 3})
                extract = evidence(record_id, "extract", {"summary": {"of": record_id}})
                record["record_schema_version"] = "1.1"
                record["design"].pop("conjugate")
                record["design"]["configuration_count"] = 2
                record["collection"]["evidence_extract"] = extract
                first_order, aperture, field = (record.pop(k) for k in ("first_order", "aperture", "field"))
                entries = []
                for cid, own, note in (("Wide", wide, "wide_note"), ("Tele", tele, "tele_note")):
                    performance = [
                        {"metric": "mtf", "value": 0.5, "basis": "ENGINE_COMPUTED",
                         "source": {**own, "section": "mtf"}},
                        {"metric": "note", "value": 1, "basis": "ENGINE_COMPUTED",
                         "source": {**shared, "section": note}},
                    ]
                    if extract_claimed_by_tele and cid == "Tele":
                        performance.append({"metric": "extra", "value": 1, "basis": "ENGINE_COMPUTED",
                                            "source": {**extract, "section": "summary"}})
                    entries.append({"configuration_id": cid, "conjugate": "INFINITE_TO_FINITE",
                                    "first_order": copy.deepcopy(first_order), "aperture": copy.deepcopy(aperture),
                                    "field": copy.deepcopy(field), "performance": performance})
                record["configurations"] = entries
            return mutate

        project = helpers.minimal_project([{"configuration_id": "Wide"}, {"configuration_id": "Tele"}])
        corpus.add("zoom", project=project, with_evidence=False, mutate=zoom_record("zoom", False))
        corpus.add("claimed", project=project, with_evidence=False, mutate=zoom_record("claimed", True))

        def phone(record: dict[str, Any]) -> None:
            record["collection"]["evidence_extract"] = evidence(
                "phone", "evidence", {"prescription": {"rows": [1]}, "stated": {"efl_mm": 1}})

        corpus.add("phone", with_evidence=False, mutate=phone)
        cls.corpus_index = Path(cls._tmp.name) / "index.sqlite"
        build_index(root, cls.corpus_index)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def index_path(self) -> Path:
        return self.corpus_index

    def path(self, name: str, record_id: str = "zoom") -> str:
        return self.paths[(record_id, name)]

    def inspect(self, record_id: str, *args: str) -> dict[str, Any]:
        return self.cli_ok("inspect", record_id, *args)["result"]

    def contents(self, result: dict[str, Any]) -> dict[str, Any]:
        return {path: entry["sections"] for path, entry in result["evidence_sections"].items()}

    def test_configuration_selects_its_own_section(self) -> None:
        for cid, name in (("Wide", "wide"), ("Tele", "tele")):
            result = self.inspect("zoom", "--configuration", cid, "--evidence", "mtf")
            self.assertEqual(self.contents(result), {self.path(name): {"mtf": {"which": cid}}})

    def test_unqualified_name_of_several_files_is_ambiguous(self) -> None:
        error = self.cli_error("EVIDENCE_AMBIGUOUS", 2, "inspect", "zoom", "--evidence", "mtf")
        self.assertEqual(error["details"]["section"], "mtf")
        self.assertEqual(error["details"]["candidates"], [
            {"path": self.path("tele"), "configurations": ["Tele"]},
            {"path": self.path("wide"), "configurations": ["Wide"]}])
        self.assertIn("path#section", error["hint"])
        self.assertIn("--configuration", error["hint"])
        result = self.inspect("zoom", "--evidence", f"{self.path('wide')}#mtf")
        self.assertEqual(self.contents(result), {self.path("wide"): {"mtf": {"which": "Wide"}}})

    def test_all_keeps_every_distinct_path(self) -> None:
        result = self.inspect("zoom", "--evidence", "all")
        self.assertEqual(self.contents(result), {
            self.path("extract"): {"summary": {"of": "zoom"}},
            self.path("shared"): {"common": 3, "tele_note": 2, "wide_note": 1},
            self.path("tele"): {"mtf": {"which": "Tele"}},
            self.path("wide"): {"mtf": {"which": "Wide"}}})
        result = self.inspect("zoom", "--configuration", "Wide", "--evidence", "all")
        self.assertEqual(self.contents(result), {
            self.path("extract"): {"summary": {"of": "zoom"}},
            self.path("shared"): {"common": 3, "wide_note": 1},
            self.path("wide"): {"mtf": {"which": "Wide"}}})

    def test_sections_of_other_configurations_do_not_apply(self) -> None:
        for wanted in ("tele_note", f"{self.path('tele')}#mtf", f"{self.path('shared')}#tele_note"):
            with self.subTest(wanted=wanted):
                self.cli_error("EVIDENCE_SECTION_NOT_FOUND", 3, "inspect", "zoom", "--configuration", "Wide",
                               "--evidence", wanted)

    def test_default_listing_is_scoped_and_names_configurations(self) -> None:
        listing = self.inspect("zoom", "--configuration", "Wide")["evidence"]
        self.assertEqual(listing["sections"], {self.path("extract"): ["summary"],
                                               self.path("shared"): ["common", "wide_note"],
                                               self.path("wide"): ["mtf"]})
        self.assertEqual({(r["path"], r["section"]) for r in listing["refs"]},
                         {(self.path("extract"), None), (self.path("shared"), "wide_note"),
                          (self.path("wide"), "mtf")})
        self.assertEqual(listing["files"], {self.path("extract"): {"configurations": []},
                                            self.path("shared"): {"configurations": ["Wide", "Tele"]},
                                            self.path("wide"): {"configurations": ["Wide"]}})
        everything = self.inspect("zoom")["evidence"]
        self.assertEqual(len(everything["sections"]), 4)
        self.assertEqual(everything["files"][self.path("tele")], {"configurations": ["Tele"]})

    def test_extract_claimed_by_another_configuration_is_excluded(self) -> None:
        listing = self.inspect("claimed", "--configuration", "Wide")["evidence"]
        extract = self.path("extract", "claimed")
        self.assertNotIn(extract, listing["sections"])
        self.assertNotIn(extract, {r["path"] for r in listing["refs"]})
        self.cli_error("EVIDENCE_SECTION_NOT_FOUND", 3, "inspect", "claimed", "--configuration", "Wide",
                       "--evidence", "summary")
        for args in (("--configuration", "Tele"), ()):
            result = self.inspect("claimed", *args, "--evidence", "summary")
            self.assertEqual(list(result["evidence_sections"]), [extract])

    def test_record_with_only_an_extract_resolves_its_sections(self) -> None:
        result = self.inspect("phone", "--evidence", "prescription")
        self.assertEqual(self.contents(result), {self.path("evidence", "phone"): {"prescription": {"rows": [1]}}})
        self.assertEqual(result["evidence"]["sections"],
                         {self.path("evidence", "phone"): ["prescription", "stated"]})


if __name__ == "__main__":
    unittest.main()

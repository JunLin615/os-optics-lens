"""Binding retrieval acceptance for the Yutong mirrorless problem 2 (SPEC amendments section 9).

Everything here runs through the production pipeline: corpus -> `build_index` -> `store` ->
`query` (and once through the CLI). Outcomes are checked against the reference evaluator in
tests/test_yutong_acceptance.py, which is loaded by file path and never modified.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from os_optics_lens import query
from os_optics_lens.build import build_index
from os_optics_lens.canonical import lf, record_hash, sha256_hex
from os_optics_lens.errors import LensError
from os_optics_lens.store import open_index

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = "acceptance/yutong-2025-mirrorless-problem-2.json"
CASE = json.loads((ROOT / FIXTURE).read_text(encoding="utf-8"))
CONSTRAINTS = {item["id"]: item for item in CASE["constraints"]}
P2B = CASE["record"]["id"]
P2B_PATH = CASE["record"]["path"]

_spec = importlib.util.spec_from_file_location("_yutong_reference_retrieval", ROOT / "tests/test_yutong_acceptance.py")
reference = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reference)

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
UNRESOLVABLE = "corner_illumination_with_unresolvable_source"
ALL_OUTCOMES = ["MATCH", "UNCONFIRMED", "DOES_NOT_MATCH"]


def load_json(path: Path) -> dict:
    return json.loads(lf(path.read_bytes()).decode("utf-8"))


P2B_RECORD = load_json(ROOT / P2B_PATH)


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


def rehash(record: dict) -> dict:
    record = copy.deepcopy(record)
    record["record_hash"] = record_hash(record)
    return record


def single_constraint_request(constraint: dict) -> dict:
    return {"mode": "specification", "filters": {"record_ids": [P2B]}, "constraints": [constraint],
            "include": ALL_OUTCOMES, "detail": "full", "evaluation_scope": "one_nominal_configuration"}


def outcome_of(entry: object) -> str:
    return entry if isinstance(entry, str) else entry["outcome"]


class _TempCorpus:
    """A copy of the checkout corpus (records/, LICENSE, licenses/, acceptance/) in a temp dir."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "corpus"
        self.root.mkdir()
        for name in ("records", "licenses", "acceptance"):
            shutil.copytree(ROOT / name, self.root / name)
        shutil.copy2(ROOT / "LICENSE", self.root / "LICENSE")
        self.out_dir = Path(self._tmp.name) / "indexes"
        self.out_dir.mkdir()

    def write_p2b(self, record: dict) -> None:
        text = json.dumps(record, indent=2, ensure_ascii=False) + "\n"
        (self.root / P2B_PATH).write_bytes(text.encode("utf-8"))

    def cleanup(self) -> None:
        self._tmp.cleanup()


_TMP: tempfile.TemporaryDirectory | None = None
INDEX_PATH: Path | None = None
INDEX = None


def setUpModule() -> None:
    global _TMP, INDEX_PATH, INDEX
    _TMP = tempfile.TemporaryDirectory()
    INDEX_PATH = Path(_TMP.name) / "index.sqlite"
    build_index(ROOT, INDEX_PATH)
    INDEX = open_index(INDEX_PATH)


def tearDownModule() -> None:
    if INDEX is not None:
        INDEX.close()
    if _TMP is not None:
        _TMP.cleanup()


class Base(unittest.TestCase):
    """Section 9.1: the fixture request on the unmodified corpus, in-process and via the CLI."""

    def check_fixture_response(self, response: dict) -> None:
        self.assertTrue(response["ok"])
        candidates = response["result"]["candidates"]
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate["record_id"], P2B)
        self.assertEqual(candidate["revision"], CASE["record"]["revision"])
        self.assertEqual(candidate["revision"], 4)
        self.assertEqual(candidate["outcome"], "MATCH")
        self.assertEqual(candidate["counts"]["MATCH"], 16)
        self.assertEqual(candidate["counts"]["MATCH"], CASE["expected"]["confirmed_constraints"])
        self.assertEqual(set(candidate["constraints"]), set(CONSTRAINTS))
        for constraint_id, entry in candidate["constraints"].items():
            self.assertEqual(outcome_of(entry), "MATCH", constraint_id)
        self.assertIn([0, CASE["expected"]["cost_proxy"]], candidate["rank"]["key"])
        for constraint_id in ("mtf_0_to_0p8", "corner_relative_illumination"):
            self.assertEqual(candidate["constraints"][constraint_id]["fact"]["source"]["status"], "RESOLVED")

    def test_fixture_adapter_request(self) -> None:
        request = query.fixture_to_request(CASE)
        self.assertEqual(request["mode"], "specification")
        self.assertEqual(request["filters"], {"record_ids": [P2B]})
        self.assertEqual(request["constraints"], CASE["constraints"])
        self.assertEqual(request["preferences"], [CASE["ranking_preference"]])
        self.assertEqual(request["evaluation_scope"], CASE["evaluation_scope"])
        self.assertEqual(request["detail"], "full")

    def test_query_search(self) -> None:
        self.check_fixture_response(query.search(INDEX, query.fixture_to_request(CASE)))

    def test_eleven_sample_mtf_grid(self) -> None:
        inspected = query.inspect(INDEX, P2B, fields=["performance.mtf_field_minimum"])["result"]
        fact = inspected["fields"]["performance.mtf_field_minimum"]["value"]
        self.assertEqual(len(fact["sampled_field_fractions"]), 11)
        self.assertEqual(fact["sampled_field_fractions"],
                         CONSTRAINTS["mtf_0_to_0p8"]["required_sampled_field_fractions"])

    def test_wheel_check_runs_all_of_item_1(self) -> None:
        # scripts/verify_wheel.py is the only clean-venv coverage of item (1) (amendment 9.4): its
        # search_fixture check must fail whenever any assertion of item (1) fails.
        spec = importlib.util.spec_from_file_location("_verify_wheel", ROOT / "scripts/verify_wheel.py")
        verify_wheel = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(verify_wheel)
        [candidate] = query.search(INDEX, query.fixture_to_request(CASE))["result"]["candidates"]
        inspected = query.inspect(INDEX, P2B, fields=["performance.mtf_field_minimum"])["result"]
        grid = inspected["fields"]["performance.mtf_field_minimum"]["value"]["sampled_field_fractions"]
        self.assertEqual(verify_wheel.acceptance_failures(CASE, candidate, grid), [])

        def broken(change) -> dict:
            mutated = copy.deepcopy(candidate)
            change(mutated)
            return mutated

        cases = {
            "preference not comparable": broken(lambda c: c["rank"].__setitem__(
                "key", ["MATCH", [1, None], P2B, 0])),
            "evidence unresolved": broken(lambda c: c["constraints"]["corner_relative_illumination"]["fact"][
                "source"].__setitem__("status", "HASH_MISMATCH")),
            "other revision": broken(lambda c: c.__setitem__("revision", 5)),
            "one constraint unconfirmed": broken(lambda c: c["constraints"].__setitem__(
                "mtf_0_to_0p8", {"outcome": "UNCONFIRMED", "reason": "SAMPLING_DIFFERS"})),
        }
        for name, mutated in cases.items():
            with self.subTest(case=name):
                self.assertTrue(verify_wheel.acceptance_failures(CASE, mutated, grid))
        for name, bad_grid in (("coarse grid", grid[::2]), ("no grid", None)):
            with self.subTest(case=name):
                self.assertTrue(verify_wheel.acceptance_failures(CASE, candidate, bad_grid))

    def test_cli_fixture(self) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
        env.pop("OS_OPTICS_LENS_INDEX", None)
        completed = subprocess.run(
            [sys.executable, "-m", "os_optics_lens", "--index", str(INDEX_PATH), "search", "--fixture",
             str(ROOT / FIXTURE)],
            capture_output=True, env=env, cwd=str(ROOT), timeout=120)
        self.assertEqual(completed.returncode, 0, completed.stdout[:2000])
        self.check_fixture_response(json.loads(completed.stdout))


class Counterexamples(unittest.TestCase):
    """Section 9.2: every fixture counterexample through a rebuilt index of a mutated corpus."""

    corpus: _TempCorpus

    @classmethod
    def setUpClass(cls) -> None:
        cls.corpus = _TempCorpus()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.corpus.cleanup()

    def build_and_search(self, record: dict, name: str, constraint: dict, *,
                         allow_unresolved_evidence: bool = False) -> dict:
        project_path = record["native_project"]["path"]
        native_bytes = lf((ROOT / project_path).read_bytes())
        if name == "a_nonblocking_stop_cannot_confirm_a_pupil_based_match":
            project = json.loads(native_bytes)
            stop = next(item["model"] for item in project["optical_models"]
                        if item["model"]["model_kind"] == "APERTURE_STOP")
            stop["edge_radius_mm"] = stop["opening_radius_mm"]
            native_bytes = json.dumps(project).encode()
            record["native_project"]["sha256"] = sha256_hex(native_bytes)
        (self.corpus.root / project_path).write_bytes(native_bytes)
        self.corpus.write_p2b(rehash(record))
        out = self.corpus.out_dir / f"{name}.sqlite"
        build_index(self.corpus.root, out, allow_unresolved_evidence=allow_unresolved_evidence)
        with open_index(out) as index:
            response = query.search(index, single_constraint_request(constraint))
        candidates = response["result"]["candidates"]
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["record_id"], P2B)
        return candidates[0]

    def test_counterexamples(self) -> None:
        self.assertEqual({case["id"] for case in CASE["counterexamples"]}, set(EXPECTED_REASONS))
        for case in CASE["counterexamples"]:
            with self.subTest(case=case["id"]):
                constraint = copy.deepcopy(CONSTRAINTS[case["constraint"]])
                record = mutate(P2B_RECORD, case)
                expected = case["expected"]
                self.assertEqual(reference.evaluate(record, constraint), expected)
                # Use the actual focal-point fact to exercise the incompatible endpoints, and set
                # a tight limit around the actual Project sensor distance to exercise shortfalls.
                if case["id"] == "rear_focal_point_cannot_replace_sensor_distance":
                    constraint["scope"] = case["copy_from"]
                if case["id"] == "comparable_back_focus_below_limit":
                    constraint["predicates"][0]["value"] = 17.062748732264
                candidate = self.build_and_search(record, case["id"], constraint,
                                                  allow_unresolved_evidence=case["id"] == UNRESOLVABLE)
                self.assertEqual(candidate["outcome"], expected)
                entry = candidate["constraints"][constraint["id"]]
                self.assertEqual(outcome_of(entry), expected)
                reason = EXPECTED_REASONS[case["id"]]
                if reason is not None:
                    self.assertEqual(entry["reason"], reason)
                if case["id"] == "comparable_back_focus_below_limit":
                    shortfall = candidate["shortfalls"][0]
                    self.assertAlmostEqual(shortfall["shortfall"], 0.01, delta=1e-9)
                    self.assertEqual(shortfall["constraint"], constraint["id"])
                if case["id"] == "mtf_limit_is_strict":
                    shortfall = candidate["shortfalls"][0]
                    self.assertEqual(shortfall["shortfall"], 0.0)
                    self.assertIs(shortfall["strict"], True)

    def test_strict_build_of_unresolvable_source_fails_and_keeps_index(self) -> None:
        case = next(item for item in CASE["counterexamples"] if item["id"] == UNRESOLVABLE)
        out = self.corpus.out_dir / "strict.sqlite"
        self.corpus.write_p2b(rehash(P2B_RECORD))
        build_index(self.corpus.root, out)
        before = out.read_bytes()
        self.corpus.write_p2b(rehash(mutate(P2B_RECORD, case)))
        with self.assertRaises(LensError) as caught:
            build_index(self.corpus.root, out)
        self.assertEqual(caught.exception.code, "BUILD_FAILED")
        codes = {finding["code"] for finding in caught.exception.details["findings"]}
        self.assertIn("EVIDENCE_FILE_MISSING", codes)
        self.assertEqual(out.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.corpus.out_dir.iterdir() if p.name.startswith("strict")),
                         ["strict.sqlite"])

    def test_spectral_weights_compare_by_ratio(self) -> None:
        record = copy.deepcopy(P2B_RECORD)
        record["spectrum"]["incident_weights"] = [2, 2, 2]
        for metric in ("mtf_field_minimum", "relative_illumination"):
            fact = next(fact for fact in record["performance"] if fact["metric"] == metric)
            fact["conditions"]["spectrum"]["incident_weights"] = [2, 2, 2]
        for name in ("spectral_sampling", "mtf_0_to_0p8", "corner_relative_illumination"):
            with self.subTest(constraint=name):
                self.assertEqual(reference.evaluate(record, CONSTRAINTS[name]), "MATCH")
                candidate = self.build_and_search(record, f"ratio-{name}", CONSTRAINTS[name])
                self.assertEqual(candidate["outcome"], "MATCH")
        record["spectrum"]["sampling_wavelengths_nm"].reverse()
        self.assertEqual(reference.evaluate(record, CONSTRAINTS["spectral_sampling"]), "DOES_NOT_MATCH")
        candidate = self.build_and_search(record, "reversed", CONSTRAINTS["spectral_sampling"])
        self.assertEqual(candidate["outcome"], "MATCH")  # Retrieval reads the native source samples.
        reversed_request = copy.deepcopy(CONSTRAINTS["spectral_sampling"])
        reversed_request["predicates"][0]["value"].reverse()
        candidate = self.build_and_search(record, "reversed-request", reversed_request)
        self.assertEqual(candidate["outcome"], "DOES_NOT_MATCH")


class Parity(unittest.TestCase):
    """Compare production and the independent evaluator on each indexed Project fact view."""

    def test_every_record_and_constraint(self) -> None:
        records = {config["record_id"]: config["view"] for config in INDEX.iter_configurations()}
        self.assertGreaterEqual(len(records), 41)
        exceptions = 0
        for record_id, record in records.items():
            request = {"mode": "specification", "filters": {"record_ids": [record_id]},
                       "constraints": CASE["constraints"], "include": ALL_OUTCOMES, "detail": "compact",
                       "max_output_bytes": 262144}
            candidates = query.search(INDEX, request)["result"]["candidates"]
            self.assertEqual(len(candidates), 1, record_id)
            produced = candidates[0]["constraints"]
            validity = record["design"].get("aperture_validity") or {}
            for constraint in CASE["constraints"]:
                with self.subTest(record=record_id, constraint=constraint["id"]):
                    expected = reference.evaluate(record, constraint)
                    entry = produced[constraint["id"]]
                    actual = outcome_of(entry)
                    if actual == expected:
                        continue
                    exceptions += 1
                    self.assertEqual(actual, "UNCONFIRMED")
                    reason = entry["reason"]
                    if reason == "STOP_MATCH_NOT_SUPPORTED":
                        self.assertEqual(expected, "MATCH")
                        self.assertIsNot(validity.get("satisfies_stop_dependent_constraints"), True)
                    else:
                        self.assertEqual(reason, "STOP_NOT_ELIGIBLE")
                        self.assertFalse(constraint.get("requires_blocking_stop"))
        # Informational: the count of documented exceptions on the current corpus.
        self.assertGreaterEqual(exceptions, 0)


if __name__ == "__main__":
    unittest.main()

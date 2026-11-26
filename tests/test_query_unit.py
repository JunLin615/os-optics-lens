"""Query layer (query.py, output.py) against an index built from the checkout corpus.

Expected counts are derived from the committed records rather than hard-coded, so new series can
be added without editing these tests.
"""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from os_optics_lens import output, query
from os_optics_lens.build import build_index
from os_optics_lens.canonical import lf
from os_optics_lens.errors import LensError
from os_optics_lens.store import open_index
from os_optics_lens.vocabulary import load_vocabulary, normalize_term

ROOT = Path(__file__).resolve().parents[1]
CASE = json.loads((ROOT / "acceptance/yutong-2025-mirrorless-problem-2.json").read_text(encoding="utf-8"))
P2B = CASE["record"]["id"]
SONNAR = "portrait-85-sonnar-eight"

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


def committed_records() -> list[dict]:
    return [json.loads(lf(path.read_bytes()).decode("utf-8"))
            for path in sorted((ROOT / "records").glob("*/*/record.json"))]


def raises(test: unittest.TestCase, code: str, fn, *args, **kwargs) -> LensError:
    with test.assertRaises(LensError) as caught:
        fn(*args, **kwargs)
    test.assertEqual(caught.exception.code, code, caught.exception.to_dict())
    return caught.exception


def check_bounds(test: unittest.TestCase, response: dict) -> bytes:
    """The wire bytes; asserts exact accounting and the budget."""
    data = output.serialize(response)
    test.assertTrue(data.endswith(b"\n"))
    data.decode("ascii")
    test.assertEqual(response["bounds"]["output_bytes"], len(data))
    test.assertLessEqual(len(data), response["bounds"]["max_output_bytes"])
    return data


def search_all(request: dict) -> list[dict]:
    """Every candidate by following cursors."""
    collected = []
    cursor = None
    while True:
        page_request = dict(request, cursor=cursor) if cursor else dict(request)
        response = query.run_request(INDEX, page_request)
        check_bounds(unittest.TestCase(), response)
        collected.extend(response["result"]["candidates"])
        cursor = response["result"]["page"]["next_cursor"]
        if cursor is None:
            return collected


class Facets(unittest.TestCase):
    def test_counts_match_committed_records(self) -> None:
        vocab = load_vocabulary()
        records = committed_records()
        camera = 0
        for record in records:
            mapping = vocab.map_lens_type(record["design"]["lens_type"]) if record["design"].get("lens_type") else None
            camera += mapping is not None and mapping.design_type == "camera_lens"
        mobile = 0
        for record in records:
            tags = {vocab.canonical_record_term("application", t)[0]
                    for t in record["design"].get("application_tags", [])}
            mapping = vocab.map_lens_type(record["design"].get("lens_type", "")) if record["design"].get("lens_type") else None
            if mapping:
                tags |= set(mapping.applications)
            mobile += "mobile_imaging" in tags
        response = query.facets(INDEX, facets=["design_type", "application"], limit_per_facet=50)
        check_bounds(self, response)
        result = response["result"]
        self.assertEqual(result["records"], len(records))
        design_types = dict(map(tuple, result["facets"]["design_type"]["values"]))
        applications = dict(map(tuple, result["facets"]["application"]["values"]))
        self.assertEqual(design_types["camera_lens"], camera)
        self.assertEqual(applications["mobile_imaging"], mobile)
        self.assertGreaterEqual(mobile, 6)
        counts = [count for _, count in result["facets"]["design_type"]["values"]]
        self.assertEqual(counts, sorted(counts, reverse=True))

    def test_alias_filter_scope_and_more(self) -> None:
        by_alias = query.facets(INDEX, filters={"facets": {"application": ["Smartphone"]}}, facets=["design_type"])
        by_term = query.facets(INDEX, filters={"facets": {"application": ["mobile_imaging"]}}, facets=["design_type"])
        self.assertEqual(by_alias["result"], by_term["result"])
        self.assertEqual(by_alias["result"]["facets"]["design_type"]["values"],
                         [["camera_lens", by_alias["result"]["records"]]])
        limited = query.facets(INDEX, facets=["family"], limit_per_facet=3)["result"]["facets"]["family"]
        self.assertEqual(len(limited["values"]), 3)
        self.assertGreater(limited["more"], 0)

    def test_facet_errors_and_empty_term(self) -> None:
        error = raises(self, "UNKNOWN_FACET", query.facets, INDEX, facets=["desgn_type"])
        self.assertIn("design_type", error.details["did_you_mean"])
        error = raises(self, "UNKNOWN_FACET_VALUE", query.facets, INDEX,
                       filters={"facets": {"design_type": ["camera_lense"]}})
        self.assertEqual(error.details["pointer"], "/filters/facets/design_type/0")
        self.assertIn("camera_lens", error.details["did_you_mean"])
        error = raises(self, "AMBIGUOUS_TERM", query.facets, INDEX, filters={"facets": {"design_type": ["objective"]}})
        self.assertEqual(error.details["pointer"], "/filters/facets/design_type/0")
        vocab = load_vocabulary()
        present = set(INDEX.facet_values("band"))
        empty = [t for t in vocab.canonical_terms("band") if t not in present]
        if empty:
            response = query.facets(INDEX, filters={"facets": {"band": [empty[0]]}}, facets=["band"])
            self.assertEqual(response["result"]["records"], 0)
            self.assertEqual(response["warnings"][0]["code"], "FACET_VALUE_EMPTY")


class Fields(unittest.TestCase):
    def test_plain_fields(self) -> None:
        response = query.fields(INDEX, limit=500)
        check_bounds(self, response)
        result = response["result"]
        efl = result["fields"]["first_order.effective_focal_length_mm"]
        expected = sum(1 for r in committed_records()
                       if isinstance(r.get("first_order", {}).get("effective_focal_length_mm"), dict))
        self.assertEqual(efl["records"], expected)
        self.assertEqual((efl["unit"], efl["value_type"], efl["stop_dependent"]), ("mm", "number", False))
        self.assertLessEqual(efl["min"], efl["max"])
        self.assertTrue(result["fields"]["performance.mtf_field_minimum"]["stop_dependent"])
        self.assertEqual(list(result["fields"]), sorted(result["fields"]))
        limited = query.fields(INDEX, limit=5)["result"]
        self.assertEqual(limited["returned"], 5)
        self.assertEqual(limited["more"], len(result["fields"]) - 5)
        scoped = query.fields(INDEX, filters={"record_ids": [P2B]}, prefix="performance.")["result"]
        self.assertTrue(all(k.startswith("performance.") for k in scoped["fields"]))
        self.assertEqual(scoped["records"], 1)

    def test_fact_description(self) -> None:
        result = query.fields(INDEX, fact="performance.mtf_field_minimum")["result"]
        self.assertTrue(result["stop_dependent"])
        self.assertIn("gt", result["applicable_ops"])
        self.assertIn("frequency_lp_mm", result["conditions_keys"])
        example = result["example_constraint"]
        response = query.run_request(INDEX, {"mode": "specification", "filters": {"record_ids": [P2B]},
                                             "constraints": [example]})
        candidate = response["result"]["candidates"][0]
        self.assertEqual(candidate["constraints"]["c0"], "MATCH")
        span = query.fields(INDEX, fact="spectrum.sampling_span_nm")["result"]
        self.assertIsNone(span["example_constraint"])
        self.assertIn("spectral_span", span["example_filter"]["filters"])
        self.assertEqual(span["eligible_for"], ["DISCOVERY_ONLY"])
        error = raises(self, "UNKNOWN_FIELD", query.fields, INDEX, fact="first_order.efl_mm")
        self.assertEqual(error.details["pointer"], "/fact")

    def test_every_example_constraint_runs(self) -> None:
        for key in INDEX.fact_keys():
            with self.subTest(fact=key):
                result = query.fields(INDEX, fact=key)["result"]
                self.assertLessEqual(len(result["variants"]), 10)
                example = result["example_constraint"]
                if example is None:
                    continue
                response = query.run_request(INDEX, {"mode": "starting_point", "constraints": [example],
                                                     "hard_shortfall": "allow", "limit": 1})
                self.assertTrue(response["ok"])


class Specification(unittest.TestCase):
    def test_yutong_fixture(self) -> None:
        request = query.fixture_to_request(CASE)
        response = query.run_request(INDEX, request)
        check_bounds(self, response)
        self.assertFalse(response["bounds"]["truncated"])
        candidates = response["result"]["candidates"]
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertNotIn("compacted", candidate)
        self.assertEqual((candidate["record_id"], candidate["revision"]), (P2B, CASE["record"]["revision"]))
        self.assertEqual(candidate["outcome"], "MATCH")
        self.assertEqual(candidate["counts"]["MATCH"], CASE["expected"]["confirmed_constraints"])
        self.assertTrue(all(item["outcome"] == "MATCH" for item in candidate["constraints"].values()))
        self.assertIn([0, CASE["expected"]["cost_proxy"]], candidate["rank"]["key"])
        for constraint_id in ("mtf_0_to_0p8", "corner_relative_illumination"):
            self.assertEqual(candidate["constraints"][constraint_id]["fact"]["source"]["status"], "RESOLVED")
        self.assertEqual(candidate["shortfalls"], [])
        self.assertEqual(candidate["unknowns"], [])
        inspected = query.inspect(INDEX, P2B, fields=["performance.mtf_field_minimum"])["result"]
        grid = inspected["fields"]["performance.mtf_field_minimum"]["value"]["sampled_field_fractions"]
        self.assertEqual(len(grid), 11)

    def test_retrieval_doc_single_query_example_runs(self) -> None:
        # docs/RETRIEVAL.md "Single query" verbatim: valid, and P2b is MATCH on every constraint.
        text = (ROOT / "docs" / "RETRIEVAL.md").read_text(encoding="utf-8").split("### Single query", 1)[1]
        request = json.loads(text.split("```json", 1)[1].split("```", 1)[0])
        response = query.run_request(INDEX, request)
        check_bounds(self, response)
        [candidate] = [c for c in response["result"]["candidates"] if c["record_id"] == P2B]
        self.assertEqual(candidate["outcome"], "MATCH")
        self.assertEqual(candidate["counts"]["MATCH"], len(request["constraints"]))
        # Empty filter lists are rejected (documented: non-empty when present), never "no filter".
        for member in ("record_ids", "collections"):
            error = raises(self, "INVALID_REQUEST", query.run_request, INDEX,
                           dict(request, filters=dict(request["filters"], **{member: []})))
            self.assertEqual(error.details["pointer"], f"/filters/{member}")

    def test_ordering_and_include(self) -> None:
        request = {"mode": "specification",
                   "constraints": [{"fact": "first_order.effective_focal_length_mm", "op": "lte", "value": 30}],
                   "preferences": [{"fact": "first_order.total_track_mm", "direction": "descending"}],
                   "limit": 50, "include": ["MATCH", "UNCONFIRMED", "DOES_NOT_MATCH"], "max_output_bytes": 262144}
        # The corpus grows with new batches and can exceed one page: follow the cursors.
        candidates = search_all(request)
        total = query.run_request(INDEX, request)["result"]["total"]
        self.assertEqual(total["listed"], len(candidates))
        order = {"MATCH": 0, "UNCONFIRMED": 1, "DOES_NOT_MATCH": 2}
        keys = [(order[c["rank"]["key"][0]], tuple((e[0], e[1] or 0) for e in c["rank"]["key"][1:-2]),
                 c["record_id"]) for c in candidates]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual([c["rank"]["position"] for c in candidates], list(range(1, len(candidates) + 1)))
        dnm = [c for c in candidates if c["outcome"] == "DOES_NOT_MATCH"]
        self.assertTrue(dnm)
        shortfall = dnm[0]["shortfalls"][0]
        self.assertAlmostEqual(shortfall["shortfall"], shortfall["value"] - 30, places=9)
        default = query.run_request(INDEX, {k: v for k, v in request.items() if k != "include"})
        self.assertEqual(default["result"]["total"]["excluded"]["outcome"], len(dnm))

    def test_unpinned_conditions(self) -> None:
        response = query.run_request(INDEX, {"mode": "specification", "filters": {"record_ids": [P2B]},
                                             "constraints": [{"fact": "performance.mtf_field_minimum", "op": "gt",
                                                              "value": 0.5, "conditions": {"frequency_lp_mm": 40}}]})
        compact = response["result"]["candidates"][0]["constraints"]["c0"]
        self.assertEqual(compact["outcome"], "MATCH")
        self.assertIn("field_end", compact["unpinned"])
        focal_length = query.run_request(INDEX, {
            "mode": "specification", "filters": {"record_ids": ["lenslibrary-1792917"]},
            "constraints": [{"fact": "first_order.effective_focal_length_mm", "op": "gt", "value": 1}],
        })
        self.assertEqual(focal_length["result"]["candidates"][0]["constraints"]["c0"]["unpinned"],
                         {"wavelength_nm": 587.7220645720764})

    def test_ranges_and_spectral_span(self) -> None:
        response = query.run_request(INDEX, {
            "mode": "starting_point", "limit": 50,
            "filters": {"ranges": [{"fact": "first_order.effective_focal_length_mm", "min": 15, "max": 30}],
                        "spectral_span": [500, 600]},
            "fields": ["first_order.effective_focal_length_mm"]})
        result = response["result"]
        self.assertGreater(result["total"]["listed"], 0)
        for candidate in result["candidates"]:
            self.assertTrue(15 <= candidate["facts"]["first_order.effective_focal_length_mm"]["value"] <= 30)
        excluded = result["total"]["excluded"]
        self.assertEqual(result["total"]["evaluated"] + sum(excluded[k] for k in ("filters", "missing_fact", "out_of_range")),
                         INDEX.record_count)
        multi = query.run_request(INDEX, {"mode": "starting_point", "filters": {
            "record_ids": [SONNAR], "ranges": [{"fact": "performance.mtf", "min": 0.9}]}})
        notes = multi["result"]["candidates"][0]["notes"]
        self.assertEqual(notes[0]["range"], "performance.mtf")
        self.assertTrue(notes[0]["multi_valued"])
        raises(self, "UNKNOWN_FIELD", query.run_request, INDEX, {
            "mode": "starting_point", "filters": {"ranges": [{"fact": "first_order.efl", "min": 1}]}})

    def test_unknown_shorthand_fact_warns(self) -> None:
        response = query.run_request(INDEX, {"mode": "specification", "filters": {"record_ids": [P2B]},
                                             "constraints": [{"fact": "first_order.effective_focal_lenght_mm",
                                                              "op": "lte", "value": 30}]})
        warning = response["warnings"][0]
        self.assertEqual((warning["code"], warning["pointer"]), ("UNKNOWN_FIELD", "/constraints/0/fact"))
        self.assertIn("first_order.effective_focal_length_mm", warning["did_you_mean"])
        self.assertEqual(response["result"]["candidates"][0]["constraints"]["c0"],
                         {"outcome": "UNCONFIRMED", "reason": "FACT_MISSING"})


class StartingPoint(unittest.TestCase):
    REQUEST = {
        "mode": "starting_point",
        "filters": {"facets": {"design_type": ["camera"]}},
        "constraints": [{"id": "circle", "fact": "image_format.image_circle_diameter_mm", "op": "gte", "value": 43},
                        {"id": "fno", "fact": "aperture.f_number", "op": "lte", "value": 4}],
        "targets": [{"id": "efl", "fact": "first_order.effective_focal_length_mm", "target": 24, "tolerance": 3}],
        "preferences": [{"fact": "first_order.total_track_mm", "direction": "ascending"}],
        "limit": 50,
    }

    def test_flexible_efl_target(self) -> None:
        response = query.run_request(INDEX, self.REQUEST)
        check_bounds(self, response)
        result = response["result"]
        candidates = result["candidates"]
        self.assertTrue(candidates)
        keys = [c["rank"]["key"] for c in candidates]
        sortable = [(k[0], k[1], k[2], k[3], tuple((e[0], e[1] or 0) for e in k[4:-2]), k[-2], k[-1]) for k in keys]
        self.assertEqual(sortable, sorted(sortable))
        for candidate in candidates:
            self.assertEqual(candidate["counts"]["DOES_NOT_MATCH"], 0)
            self.assertTrue(candidate["rank"]["explanation"].endswith("."))
            deviations = [s for s in candidate["shortfalls"] if s.get("target") == "efl"]
            unknown_targets = [u for u in candidate["unknowns"] if u.get("target") == "efl"]
            self.assertEqual(len(deviations) + len(unknown_targets), 1)
            if deviations:
                deviation = deviations[0]
                self.assertAlmostEqual(deviation["normalized"], abs(deviation["value"] - 24) / 3, places=9)
                self.assertEqual(candidate["rank"]["key"][3], round(deviation["normalized"], 9))
            for unknown in candidate["unknowns"]:
                self.assertIn("reason", unknown)
        p2b = next(c for c in candidates if c["record_id"] == P2B)
        self.assertEqual(p2b["constraints"]["circle"], "MATCH")
        self.assertEqual(p2b["constraints"]["fno"]["reason"], "FACT_MISSING")
        self.assertIn("first_order.effective_focal_length_mm", p2b["facts"])
        excluded = result["total"]["excluded"]["hard_shortfall"]
        allowed = query.run_request(INDEX, dict(self.REQUEST, hard_shortfall="allow"))["result"]
        self.assertEqual(allowed["total"]["listed"], result["total"]["listed"] + excluded)
        if excluded:
            worst = allowed["candidates"][-1]
            self.assertGreater(worst["counts"]["DOES_NOT_MATCH"], 0)
            self.assertTrue(any("shortfall" in s for s in worst["shortfalls"]))

    def test_target_scale_bases_and_full_detail(self) -> None:
        request = {"mode": "starting_point", "filters": {"record_ids": [P2B]}, "detail": "full",
                   "targets": [{"id": "rel", "fact": "first_order.effective_focal_length_mm", "target": 24,
                                "relative_tolerance": 0.1},
                               {"id": "plain", "fact": "first_order.total_track_mm", "target": 100}]}
        candidate = query.run_request(INDEX, request)["result"]["candidates"][0]
        bases = {s["target"]: s["scale_basis"] for s in candidate["shortfalls"]}
        self.assertEqual(bases, {"rel": "relative_tolerance", "plain": "target"})
        self.assertIn("ENGINE_COMPUTED", candidate["facts"]["first_order.effective_focal_length_mm"]["basis"])
        raises(self, "INVALID_REQUEST", query.run_request, INDEX, {
            "mode": "starting_point", "targets": [{"fact": "spectrum.sampling_span_nm", "target": 500}]})


    def test_weighted_score_is_reconstructible_from_the_output(self) -> None:
        request = {"mode": "starting_point", "filters": {"record_ids": [P2B]},
                   "targets": [{"id": "efl", "fact": "first_order.effective_focal_length_mm", "target": 50,
                                "tolerance": 10},
                               {"id": "hf", "fact": "field.half_field_deg", "target": 20, "relative_tolerance": 0.2,
                                "weight": 2}]}
        [candidate] = query.run_request(INDEX, request)["result"]["candidates"]
        deviations = {s["target"]: s for s in candidate["shortfalls"] if "target" in s}
        self.assertEqual({k: v["weight"] for k, v in deviations.items()}, {"efl": 1, "hf": 2})
        score = round(sum(s["weight"] * s["normalized"] for s in deviations.values()), 9)
        self.assertEqual(candidate["rank"]["key"][3], score)
        explanation = candidate["rank"]["explanation"]
        self.assertIn(f"(2 x {deviations['hf']['normalized']:.3g} x relative_tolerance)", explanation)
        self.assertIn(f"({deviations['efl']['normalized']:.3g} x tolerance)", explanation)
        self.assertIn(f"target score {score:g}", explanation)

    def test_tiny_target_scale_is_invalid_request_not_internal(self) -> None:
        efl = "first_order.effective_focal_length_mm"
        cases = (({"fact": efl, "target": 1e-320}, "/targets/0/target"),
                 ({"fact": efl, "target": 24, "tolerance": 1e-310}, "/targets/0/tolerance"),
                 ({"fact": efl, "target": 1e300, "tolerance": 1e-10}, "/targets/0"))
        for target, pointer in cases:
            with self.subTest(target=target):
                error = raises(self, "INVALID_REQUEST", query.run_request, INDEX,
                               {"mode": "starting_point", "targets": [target], "limit": 1})
                self.assertEqual(error.details["pointer"], pointer)
        heavy = [{"fact": efl, "target": 1, "weight": 1e308},
                 {"fact": "first_order.total_track_mm", "target": 1, "weight": 1e308}]
        error = raises(self, "INVALID_REQUEST", query.run_request, INDEX,
                       {"mode": "starting_point", "filters": {"record_ids": [P2B]}, "targets": heavy})
        self.assertEqual(error.details["pointer"], "/targets")


class Compare(unittest.TestCase):
    def test_compare_rows(self) -> None:
        response = query.compare(INDEX, [P2B, SONNAR, "lenslibrary-1792917"])
        check_bounds(self, response)
        rows = {row["fact"]: row for row in response["result"]["rows"]}
        self.assertEqual([c["record_id"] for c in response["result"]["candidates"]],
                         [P2B, SONNAR, "lenslibrary-1792917"])
        efl = rows["first_order.effective_focal_length_mm"]
        self.assertTrue(all(cell is not None for cell in efl["values"]))
        self.assertFalse(efl["comparable"])
        self.assertIn("conditions", efl["differs_by"])
        self.assertTrue(rows["first_order.total_track_mm"]["comparable"])
        pair = query.compare(INDEX, [P2B, SONNAR], fields=["first_order.total_track_mm", "performance.mtf"])
        pair_rows = {row["fact"]: row for row in pair["result"]["rows"]}
        self.assertTrue(pair_rows["first_order.total_track_mm"]["comparable"])
        self.assertIn("multi_valued", pair_rows["performance.mtf"]["differs_by"])

    def test_compare_errors(self) -> None:
        raises(self, "UNKNOWN_FIELD", query.compare, INDEX, [P2B, SONNAR], fields=["first_order.efl"])
        raises(self, "INVALID_REQUEST", query.compare, INDEX, [P2B] * 2)
        # `ID@` is not in the REF grammar and must not alias the default configuration of `ID`.
        error = raises(self, "INVALID_REQUEST", query.compare, INDEX, [P2B, f"{P2B}@"])
        self.assertEqual(error.details["pointer"], "/refs/1")
        error = raises(self, "INVALID_REQUEST", query.compare, INDEX, [f"{P2B}@", P2B])
        self.assertEqual(error.details["pointer"], "/refs/0")
        raises(self, "INVALID_REQUEST", query.compare, INDEX, [f"r{i}" for i in range(11)])
        error = raises(self, "CONFIGURATION_NOT_FOUND", query.compare, INDEX, [P2B, f"{SONNAR}@Wide"])
        self.assertEqual(error.details["pointer"], "/refs/1")
        error = raises(self, "RECORD_NOT_FOUND", query.compare, INDEX, ["yutong-2025-mirrorless-18-p2", SONNAR])
        self.assertIn(P2B, error.details["did_you_mean"])


class Inspect(unittest.TestCase):
    def test_default(self) -> None:
        response = query.inspect(INDEX, P2B)
        check_bounds(self, response)
        result = response["result"]
        self.assertEqual(result["revision"], CASE["record"]["revision"])
        self.assertIn("performance.mtf_field_minimum", result["fact_keys"])
        self.assertIn("field_performance", result["evidence"]["sections"][
            f"records/yutong-cup/{P2B}/evidence.json"])
        self.assertEqual(result["configuration"]["configuration_id"], None)
        self.assertNotIn("fields", result)
        self.assertNotIn("license", result)
        self.assertNotIn("provenance", result)
        self.assertEqual(query.inspect(INDEX, P2B, provenance=True)["result"]["provenance"]["license"]["spdx"], "MIT")

    def test_fields_evidence_prescription(self) -> None:
        result = query.inspect(INDEX, P2B, fields=["first_order", "aperture.stop_opening_diameter_mm"])["result"]
        fields = result["fields"]
        self.assertNotIn("checks", fields["first_order.back_focal_length_mm"])
        self.assertTrue(fields["aperture.stop_opening_diameter_mm"]["system_aperture"])
        self.assertAlmostEqual(fields["aperture.stop_opening_diameter_mm"]["value"]["value"], 24.852063868184835)
        evidence = query.inspect(INDEX, P2B, evidence=["back_focus"])["result"]["evidence_sections"]
        section = next(iter(evidence.values()))["sections"]["back_focus"]
        self.assertIn("value_mm", section)
        error = raises(self, "EVIDENCE_SECTION_NOT_FOUND", query.inspect, INDEX, P2B, evidence=["nope"])
        self.assertIn("back_focus", error.details["available"])
        summary = query.inspect(INDEX, P2B, prescription="summary")["result"]["prescription"]
        self.assertTrue(summary["optical_models"])
        self.assertTrue(summary["materials"])
        error = raises(self, "OUTPUT_LIMIT_EXCEEDED", query.inspect, INDEX, P2B, prescription="full",
                       evidence=["all"])
        self.assertGreater(error.details["required_bytes"], 32768)
        full = query.inspect(INDEX, P2B, prescription="full", evidence=["all"], max_output_bytes=262144)
        check_bounds(self, full)
        self.assertIn("optical_models", full["result"]["prescription"])

    def test_errors(self) -> None:
        error = raises(self, "REVISION_NOT_AVAILABLE", query.inspect, INDEX, P2B, revision=1)
        self.assertEqual(error.details["available_revision"], CASE["record"]["revision"])
        raises(self, "RECORD_NOT_FOUND", query.inspect, INDEX, "no-such-record")
        raises(self, "CONFIGURATION_NOT_FOUND", query.inspect, INDEX, P2B, configuration="Tele")
        raises(self, "UNKNOWN_FIELD", query.inspect, INDEX, P2B, fields=["first_order.efl"])
        raises(self, "UNKNOWN_FIELD", query.inspect, INDEX, P2B, sections=["firstorder"])


class Project(unittest.TestCase):
    def test_every_record_hands_off_its_project(self) -> None:
        records = committed_records()
        self.assertEqual(len(records), INDEX.record_count)
        for record in records:
            with self.subTest(record=record["record_id"]):
                meta = query.project_metadata(INDEX, record["record_id"])
                data = query.project_bytes(INDEX, record["record_id"], revision=record["record_revision"])
                self.assertEqual(meta["sha256"], record["native_project"]["sha256"])
                self.assertEqual(hashlib.sha256(data).hexdigest(), record["native_project"]["sha256"])
                self.assertEqual(meta["size_bytes"], len(data))
                self.assertEqual(meta["revision"], record["record_revision"])
                self.assertTrue(meta["configurations"])
                self.assertNotIn("license", meta)
                self.assertNotIn("attribution", meta)
        response = query.project_response(INDEX, P2B, extra={"path": "/tmp/x.json"})
        check_bounds(self, response)
        self.assertEqual(response["result"]["path"], "/tmp/x.json")
        raises(self, "REVISION_NOT_AVAILABLE", query.project_bytes, INDEX, P2B, revision=99)


class Pagination(unittest.TestCase):
    REQUEST = {"mode": "starting_point",
               "targets": [{"fact": "first_order.effective_focal_length_mm", "target": 50}],
               "limit": 7}

    def test_pages_concatenate_to_the_full_list(self) -> None:
        # Pages of the largest size and pages of seven give the same list, however large the corpus.
        large = dict(self.REQUEST, limit=50, max_output_bytes=262144)
        listed = query.run_request(INDEX, large)["result"]["total"]["listed"]
        full = search_all(large)
        pages = search_all(self.REQUEST)
        self.assertEqual(pages, full)
        self.assertEqual(len(pages), listed)
        again = search_all(self.REQUEST)
        self.assertEqual(pages, again)
        first = query.run_request(INDEX, self.REQUEST)
        cursor = first["result"]["page"]["next_cursor"]
        other_limit = query.run_request(INDEX, dict(self.REQUEST, limit=3, cursor=cursor))
        self.assertEqual(other_limit["result"]["candidates"], full[7:10])

    def test_cursor_errors(self) -> None:
        cursor = query.run_request(INDEX, self.REQUEST)["result"]["page"]["next_cursor"]
        changed = dict(self.REQUEST, filters={"facets": {"design_type": ["camera_lens"]}}, cursor=cursor)
        raises(self, "CURSOR_MISMATCH", query.run_request, INDEX, changed)
        raises(self, "CURSOR_INVALID", query.run_request, INDEX, dict(self.REQUEST, cursor="not-a-cursor!"))
        raises(self, "CURSOR_INVALID", query.run_request, INDEX, dict(self.REQUEST, cursor=12))
        digest = query.normalize_request(INDEX, self.REQUEST)["query_digest"]
        stale = output.encode_cursor("0" * 16, digest, 7)
        error = raises(self, "SNAPSHOT_MISMATCH", query.run_request, INDEX, dict(self.REQUEST, cursor=stale))
        self.assertEqual(error.exit_code, 4)
        beyond = output.encode_cursor(INDEX.snapshot_id, digest, 10_000)
        page = query.run_request(INDEX, dict(self.REQUEST, cursor=beyond))["result"]
        self.assertEqual((page["candidates"], page["page"]["next_cursor"]), ([], None))

    def test_explain_request_checks_the_cursor(self) -> None:
        # --explain-request reports the same cursor errors the search would, and the offset.
        raises(self, "CURSOR_INVALID", query.explain_request, INDEX, dict(self.REQUEST, cursor="garbage"))
        raises(self, "CURSOR_INVALID", query.normalize_request, INDEX, dict(self.REQUEST, cursor="garbage"))
        digest = query.normalize_request(INDEX, self.REQUEST)["query_digest"]
        stale = output.encode_cursor("0" * 16, digest, 7)
        raises(self, "SNAPSHOT_MISMATCH", query.explain_request, INDEX, dict(self.REQUEST, cursor=stale))
        cursor = query.run_request(INDEX, self.REQUEST)["result"]["page"]["next_cursor"]
        changed = dict(self.REQUEST, filters={"facets": {"design_type": ["camera_lens"]}}, cursor=cursor)
        raises(self, "CURSOR_MISMATCH", query.explain_request, INDEX, changed)
        explained = query.explain_request(INDEX, dict(self.REQUEST, cursor=cursor))["result"]
        self.assertEqual(explained["options"]["cursor_offset"], 7)
        self.assertEqual(explained["options"]["cursor"], cursor)
        error = raises(self, "CURSOR_INVALID", query.normalize_request, INDEX,
                       {"queries": [self.REQUEST, dict(self.REQUEST, cursor="garbage")]})
        self.assertEqual(error.details["pointer"], "/queries/1/cursor")
        self.assertNotIn("cursor_offset", query.normalize_request(INDEX, self.REQUEST)["options"])

    def test_digest_covers_normalized_query(self) -> None:
        alias = dict(self.REQUEST, filters={"facets": {"application": ["smartphone"]}})
        term = dict(self.REQUEST, filters={"facets": {"application": ["mobile_imaging"]}}, limit=2, detail="full")
        self.assertEqual(query.normalize_request(INDEX, alias)["query_digest"],
                         query.normalize_request(INDEX, term)["query_digest"])


class Budget(unittest.TestCase):
    QUERIES = [
        {"mode": "starting_point", "targets": [{"fact": "first_order.effective_focal_length_mm", "target": 24}],
         "limit": 20},
        {"mode": "specification", "constraints": [{"fact": "aperture.f_number", "op": "lte", "value": 5}],
         "limit": 20, "detail": "full"},
        query.fixture_to_request(CASE),
        {"mode": "starting_point", "filters": {"facets": {"design_type": ["microscope_objective"]}}, "limit": 20},
    ]

    def test_budget_never_exceeded_and_progress(self) -> None:
        for budget in (2048, 3000, 4096, 6000, 9000, 16384, 32768, 65536):
            with self.subTest(budget=budget):
                try:
                    response = query.run_request(INDEX, {"queries": self.QUERIES, "max_output_bytes": budget})
                except LensError as error:
                    self.assertEqual(error.code, "OUTPUT_LIMIT_EXCEEDED")
                    self.assertGreater(error.details["required_bytes"], budget)
                    continue
                check_bounds(self, response)
                for entry in response["results"]:
                    if not entry["ok"]:
                        self.assertEqual(entry["error"]["code"], "OUTPUT_LIMIT_EXCEEDED")
                        continue
                    page = entry["result"]["page"]
                    listed = entry["result"]["total"]["listed"]
                    if listed > page["offset"]:
                        self.assertGreaterEqual(page["returned"], 1)
                    if page["returned"] < min(page["limit"], listed - page["offset"]):
                        self.assertTrue(response["bounds"]["truncated"])
                        self.assertIsNotNone(page["next_cursor"])

    def test_round_robin_and_continuation(self) -> None:
        response = query.run_request(INDEX, {"queries": self.QUERIES[:2], "max_output_bytes": 6000})
        check_bounds(self, response)
        self.assertTrue(response["bounds"]["truncated"])
        returned = [entry["result"]["page"]["returned"] for entry in response["results"]]
        self.assertTrue(all(n >= 1 for n in returned))
        first = response["results"][0]["result"]
        follow = query.run_request(INDEX, dict(self.QUERIES[0], cursor=first["page"]["next_cursor"]))
        self.assertEqual(follow["result"]["candidates"][0]["rank"]["position"], first["page"]["returned"] + 1)

    def test_skeleton_reservation(self) -> None:
        many = [copy.deepcopy(self.QUERIES[0]) for _ in range(20)]
        error = raises(self, "OUTPUT_LIMIT_EXCEEDED", query.run_request, INDEX,
                       {"queries": many, "max_output_bytes": 2048})
        self.assertGreater(error.details["required_bytes"], 2048)

    def test_compact_fallback_for_first_candidate(self) -> None:
        request = query.fixture_to_request(CASE)
        full_size = query.run_request(INDEX, request)["bounds"]["output_bytes"]
        response = query.run_request(INDEX, dict(request, max_output_bytes=max(2048, full_size - 200)))
        check_bounds(self, response)
        candidate = response["result"]["candidates"][0]
        self.assertTrue(candidate["compacted"])
        self.assertNotIn("facts", candidate)
        self.assertEqual(candidate["counts"]["MATCH"], 16)
        self.assertIsNone(response["result"]["page"]["next_cursor"])
        self.assertTrue(response["bounds"]["truncated"])

    def test_oversized_query_fails_alone_wherever_it_is(self) -> None:
        # A query whose compact first candidate cannot fit even alone fails by itself; a small
        # query before or after it keeps its candidate and the batch stays ok (amendment 10.14).
        big = {"mode": "specification", "filters": {"record_ids": [P2B]}, "limit": 1,
               "constraints": [{"id": f"c{i}", "fact": "first_order.effective_focal_length_mm", "op": "gte",
                                "value": 0} for i in range(200)]}
        small = {"mode": "starting_point", "limit": 1}
        alone = query.run_request(INDEX, {"queries": [small], "max_output_bytes": 8192})
        self.assertLess(alone["bounds"]["output_bytes"], 4096)
        for order in ((big, small), (small, big)):
            with self.subTest(first=order[0]["mode"]):
                response = query.run_request(INDEX, {"queries": list(order), "max_output_bytes": 8192})
                check_bounds(self, response)
                self.assertTrue(response["ok"])
                self.assertEqual(output.exit_code(response), 0)
                by_mode = {q["mode"]: entry for q, entry in zip(order, response["results"])}
                self.assertTrue(by_mode["starting_point"]["ok"])
                self.assertEqual(by_mode["starting_point"]["result"]["page"]["returned"], 1)
                failed = by_mode["specification"]
                self.assertFalse(failed["ok"])
                self.assertEqual(failed["error"]["code"], "OUTPUT_LIMIT_EXCEEDED")
                self.assertGreater(failed["error"]["details"]["required_bytes"], 8192)

    def test_batch_error_entries_clip_echoed_values(self) -> None:
        key = "bogus_" + "k" * 300000
        response = query.run_request(INDEX, {"queries": [{"mode": "x" * 300000}, {"mode": "specification", key: 1},
                                                         self.QUERIES[0]], "max_output_bytes": 4096})
        check_bounds(self, response)
        self.assertTrue(response["ok"])
        mode, unknown = response["results"][0]["error"], response["results"][1]["error"]
        self.assertEqual((mode["code"], mode["details"]["pointer"]), ("INVALID_REQUEST", "/queries/0/mode"))
        self.assertLess(len(mode["message"]), 300)
        self.assertEqual(unknown["code"], "INVALID_REQUEST")
        self.assertTrue(unknown["details"]["truncated"])
        self.assertLess(len(json.dumps(unknown)), 2048)
        self.assertGreaterEqual(response["results"][2]["result"]["page"]["returned"], 1)

    def test_batch_errors_are_entries(self) -> None:
        response = query.run_request(INDEX, {"queries": [self.QUERIES[0], {"mode": "specification"}]})
        self.assertTrue(response["ok"])
        bad = response["results"][1]
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["error"]["details"]["pointer"], "/queries/1/constraints")
        failed = query.run_request(INDEX, {"queries": [{"mode": "x"}]})
        self.assertFalse(failed["ok"])
        self.assertEqual(output.exit_code(failed), 2)
        check_bounds(self, failed)


class ClosedSchema(unittest.TestCase):
    def expect(self, request: dict, code: str, pointer: str) -> LensError:
        error = raises(self, code, query.run_request, INDEX, request)
        self.assertEqual(error.details.get("pointer"), pointer, error.to_dict())
        return error

    def test_pointers_and_suggestions(self) -> None:
        base = {"mode": "specification", "constraints": [{"fact": "first_order.effective_focal_length_mm",
                                                          "op": "lte", "value": 30}]}
        error = self.expect(dict(base, constraint=[]), "INVALID_REQUEST", "/constraint")
        self.assertIn("constraints", error.details["did_you_mean"])
        self.expect(dict(base, filters={"facet": {}}), "INVALID_REQUEST", "/filters/facet")
        self.expect({"mode": "specification", "constraints": [
            {"scope": ["first_order"], "predicates": [{"path": "x", "op": "eq", "value": 1, "oops": 1}]}]},
            "INVALID_REQUEST", "/constraints/0/predicates/0/oops")
        error = self.expect({"mode": "specification", "constraints": [{"fact": "first_order.total_track_mm",
                                                                       "op": "lessthan", "value": 1}]},
                            "UNSUPPORTED_OPERATOR", "/constraints/0/op")
        self.assertEqual(error.exit_code, 2)
        self.expect({"constraints": []}, "INVALID_REQUEST", "/mode")
        self.expect({"mode": "specification"}, "INVALID_REQUEST", "/constraints")
        error = self.expect(dict(base, preferences={"fact": "first_order.total_track_mm", "direction": "ascending"}),
                            "INVALID_REQUEST", "/preferences")
        self.assertIn("list", error.hint)
        error = self.expect(dict(base, limit=51), "INVALID_REQUEST", "/limit")
        self.assertEqual(error.details["allowed"], {"min": 1, "max": 50})
        self.expect(dict(base, max_output_bytes=100), "INVALID_REQUEST", "/max_output_bytes")
        self.expect(dict(base, targets=[{"fact": "first_order.total_track_mm", "target": 1}]),
                    "INVALID_REQUEST", "/targets")
        self.expect(dict(base, evaluation_scope="all_configurations"), "INVALID_REQUEST", "/evaluation_scope")
        self.expect(dict(base, fields=["first_order.efl"]), "UNKNOWN_FIELD", "/fields/0")
        self.expect({"mode": "specification", "constraints": [{"fact": "performance.distortion_range", "op": "lte",
                                                               "value": 1}]},
                    "INVALID_REQUEST", "/constraints/0/fact")
        self.expect({"queries": [base], "max_output_bytes": 4096, "extra": 1}, "INVALID_REQUEST", "/extra")
        response = query.run_request(INDEX, {"queries": [dict(base, max_output_bytes=4096)]})
        self.assertEqual(response["results"][0]["error"]["details"]["pointer"], "/queries/0/max_output_bytes")
        raises(self, "INVALID_REQUEST", query.run_request, INDEX, [base])


class Serialization(unittest.TestCase):
    def test_error_envelope_is_bounded(self) -> None:
        cases = (
            LensError("INVALID_REQUEST", "unknown mode " + repr("x" * 300000), hint="h" * 5000,
                      details={"pointer": "/mode", "did_you_mean": ["y" * 100000], "actual": ["z"] * 50000}),
            LensError("BUILD_FAILED", "index build failed", hint="fix them", details={
                "errors": 80, "findings_omitted": 30,
                "findings": [{"severity": "error", "code": "INVALID_FIELD", "file": f"records/c/r{i}/record.json",
                              "field": "design", "message": "design must be an object", "expected": "an object",
                              "actual": [{"lens_type": "camera" * 50}] * 300} for i in range(50)]}),
        )
        for error in cases:
            for budget in (2048, 4096, 32768):
                with self.subTest(code=error.code, budget=budget):
                    envelope = output.bounded_error_response(error, budget)
                    self.assertLessEqual(len(output.serialize(envelope)), budget)
                    self.assertEqual(envelope["error"]["code"], error.code)
                    self.assertEqual(set(envelope["error"]), {"code", "message", "hint", "details"})
                    details = envelope["error"]["details"]
                    self.assertTrue(details["truncated"])
                    if error.code == "BUILD_FAILED":
                        self.assertEqual(len(details["findings"]) + details["findings_omitted"], 80)
                    else:
                        self.assertEqual(details["pointer"], "/mode")
        small = LensError("RECORD_NOT_FOUND", "no record 'x'", details={"did_you_mean": []})
        self.assertEqual(output.bounded_error_response(small, 2048), output.error_response(small))

    def test_ascii_wire_form_and_finalize(self) -> None:
        response = output.finalize({"ok": True, "result": {"title": "36 × 24 mm"}}, 2048)
        data = check_bounds(self, response)
        self.assertIn(b"\\u00d7", data)
        big = {"ok": True, "result": "x" * 5000}
        error = raises(self, "OUTPUT_LIMIT_EXCEEDED", output.finalize, big, 2048)
        self.assertEqual(error.details["required_bytes"], len(output.serialize(big)))

    def test_output_bytes_digit_boundary(self) -> None:
        for size in range(9900, 10100, 7):
            response = output.finalize({"ok": True, "result": "y" * size}, 262144)
            check_bounds(self, response)

    def test_exit_codes(self) -> None:
        self.assertEqual(output.exit_code({"ok": True}), 0)
        self.assertEqual(output.exit_code(output.error_response(LensError("SNAPSHOT_MISMATCH", "x"))), 4)

    def test_normalize_term_consistency(self) -> None:
        self.assertEqual(normalize_term("Cell Phone"), "cell_phone")


if __name__ == "__main__":
    unittest.main()

"""Real queries across the committed corpus (section 9 `test_search_lenslibrary`).

Expected values are derived from the committed records, so new series do not require edits here.
"""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from os_optics_lens import output, query
from os_optics_lens.build import build_index
from os_optics_lens.canonical import lf
from os_optics_lens.store import open_index

ROOT = Path(__file__).resolve().parents[1]
EFL = "first_order.effective_focal_length_mm"

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


def lenslibrary_records() -> list[dict]:
    return [json.loads(lf(p.read_bytes()).decode("utf-8"))
            for p in sorted((ROOT / "records/lenslibrary").glob("*/record.json"))]


def wire(response: dict) -> bytes:
    data = output.serialize(response)
    assert response["bounds"]["output_bytes"] == len(data)
    assert len(data) <= response["bounds"]["max_output_bytes"]
    return data


class Discovery(unittest.TestCase):
    def test_collection_and_lens_type_counts(self) -> None:
        records = lenslibrary_records()
        result = query.facets(INDEX, {"facets": {"collection": ["lenslibrary"]}},
                              ["source_lens_type", "collection"])["result"]
        self.assertEqual(result["records"], len(records))
        counts = dict(map(tuple, result["facets"]["source_lens_type"]["values"]))
        expected = Counter(r["design"]["lens_type"] for r in records if r["design"].get("lens_type") is not None)
        self.assertEqual(counts, dict(expected))

    def test_fields_cover_every_record(self) -> None:
        result = query.fields(INDEX, {"facets": {"collection": ["lenslibrary"]}}, limit=500)["result"]
        efl = result["fields"][EFL]
        self.assertEqual(efl["records"], len(lenslibrary_records()))
        values = [r["first_order"]["engine_check"]["effective_focal_length_mm"] for r in lenslibrary_records()]
        self.assertEqual((efl["min"], efl["max"]), (min(values), max(values)))
        self.assertEqual(efl["unit"], "mm")


class Searches(unittest.TestCase):
    def test_microscope_starting_point_is_explainable(self) -> None:
        request = {"mode": "starting_point", "filters": {"facets": {"design_type": ["microscope_objective"]}},
                   "targets": [{"fact": "aperture.object_numerical_aperture", "target": 0.5, "tolerance": 0.1}],
                   "preferences": [{"fact": "first_order.total_track_mm", "direction": "ascending"}],
                   "limit": 50, "max_output_bytes": 262144}
        response = query.search(INDEX, request)
        wire(response)
        candidates = response["result"]["candidates"]
        self.assertEqual(len(candidates), response["result"]["total"]["listed"])
        self.assertTrue(candidates)
        keys = [c["rank"]["key"] for c in candidates]
        comparable = [(k[0], k[1], k[2], k[3], tuple(k[4]) if k[4][1] is not None else (1, 0.0), k[5]) for k in keys]
        self.assertEqual(comparable, sorted(comparable))
        for candidate in candidates:
            self.assertTrue(candidate["rank"]["explanation"])
            reasons = {u.get("target") or u.get("preference"): u["reason"] for u in candidate["unknowns"]}
            if candidate["shortfalls"]:
                deviation = candidate["shortfalls"][0]
                self.assertAlmostEqual(deviation["normalized"], abs(deviation["value"] - 0.5) / 0.1, places=9)
            else:
                self.assertEqual(reasons.get("t0"), "FACT_MISSING")

    def test_endoscope_specification(self) -> None:
        request = {"mode": "specification", "filters": {"facets": {"application": ["endoscopy"]}},
                   "constraints": [{"fact": "field.half_field_deg", "op": "gte", "value": 40}],
                   "include": ["MATCH", "UNCONFIRMED", "DOES_NOT_MATCH"], "limit": 50}
        result = query.search(INDEX, request)["result"]
        self.assertTrue(result["candidates"])
        for candidate in result["candidates"]:
            self.assertEqual(candidate["outcome"], "UNCONFIRMED")
            self.assertEqual(candidate["constraints"]["c0"]["reason"], "FACT_MISSING")

    def test_compare_and_inspect(self) -> None:
        ids = [r["record_id"] for r in lenslibrary_records()[:3]]
        rows = query.compare(INDEX, ids, fields=[EFL])["result"]["rows"]
        self.assertEqual(len(rows[0]["values"]), 3)
        inspected = query.inspect(INDEX, ids[0])["result"]
        self.assertIn(EFL, inspected["fact_keys"])

    def test_every_lenslibrary_project_hand_off(self) -> None:
        for record in lenslibrary_records():
            with self.subTest(record=record["record_id"]):
                data = query.project_bytes(INDEX, record["record_id"])
                self.assertEqual(hashlib.sha256(data).hexdigest(), record["native_project"]["sha256"])


class BatchBudget(unittest.TestCase):
    QUERIES = [
        {"mode": "starting_point", "filters": {"facets": {"design_type": ["camera_lens"]}},
         "targets": [{"fact": EFL, "target": 50, "relative_tolerance": 0.2}]},
        {"mode": "specification", "filters": {"facets": {"design_type": ["microscope_objective"]}},
         "constraints": [{"fact": "aperture.object_numerical_aperture", "op": "gte", "value": 0.4}],
         "include": ["MATCH", "UNCONFIRMED", "DOES_NOT_MATCH"]},
        {"mode": "starting_point", "filters": {"facets": {"application": ["endoscopy"]}},
         "targets": [{"fact": "field.half_field_deg", "target": 70, "tolerance": 10}]},
    ]

    def test_small_budget_makes_progress_on_every_query(self) -> None:
        response = query.run_request(INDEX, {"queries": self.QUERIES, "max_output_bytes": 4096})
        wire(response)
        for entry in response["results"]:
            self.assertTrue(entry["ok"], entry)
            self.assertGreaterEqual(entry["result"]["page"]["returned"], 1)

    def test_continuation_concatenates_to_full_lists(self) -> None:
        collected: list[list[str]] = [[] for _ in self.QUERIES]
        cursors: list[str | None] = [None] * len(self.QUERIES)
        pending = list(range(len(self.QUERIES)))
        for _ in range(200):
            if not pending:
                break
            batch = []
            for i in pending:
                q = copy.deepcopy(self.QUERIES[i])
                if cursors[i]:
                    q["cursor"] = cursors[i]
                batch.append(q)
            response = query.run_request(INDEX, {"queries": batch, "max_output_bytes": 4096})
            wire(response)
            still = []
            for i, entry in zip(pending, response["results"]):
                self.assertTrue(entry["ok"], entry)
                result = entry["result"]
                self.assertGreaterEqual(result["page"]["returned"], 1)
                collected[i].extend(c["record_id"] for c in result["candidates"])
                cursors[i] = result["page"]["next_cursor"]
                if cursors[i] is not None:
                    still.append(i)
            pending = still
        self.assertEqual(pending, [])
        for i, q in enumerate(self.QUERIES):
            full = query.search(INDEX, dict(q, limit=50, max_output_bytes=262144))["result"]["candidates"]
            self.assertEqual(collected[i], [c["record_id"] for c in full])


if __name__ == "__main__":
    unittest.main()

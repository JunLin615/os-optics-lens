"""Search-level regressions for ranking with actual sampled field grids."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

import test_build_foundation as helpers
from os_optics_lens import query
from os_optics_lens.build import build_index
from os_optics_lens.store import open_index

MTF = "performance.mtf_field_minimum"
GRID = [0, 0.4, 0.8]
OTHER_GRID = [0, 0.5, 0.8]


def mtf(value: float, grid: list[float] | None = GRID, *, frequency: int = 40,
        weights: list[int] | None = None, orientation: str = "sagittal") -> dict[str, Any]:
    entry = {"metric": "mtf_field_minimum", "value": value, "basis": "ENGINE_COMPUTED",
             "conditions": {"frequency_lp_mm": frequency, "field_definition": "normalized image height",
                            "spectrum": {"sampling_wavelengths_nm": [450, 650],
                                         "incident_weights": weights if weights is not None else [1, 2]}},
             "orientation": orientation}
    if grid is not None:
        entry["sampled_field_fractions"] = grid
    return entry


class SampledGridRanking(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temp.cleanup)
        directory = Path(temp.name)
        corpus = helpers.SynthCorpus(directory / "corpus")

        def lens(record_id: str, *entries: dict[str, Any]) -> None:
            def mutate(record: dict[str, Any]) -> None:
                record["performance"] = list(entries)
                record["design"]["aperture_validity"]["satisfies_stop_dependent_constraints"] = True
            corpus.add(record_id, with_evidence=False, mutate=mutate)

        def control(value: float) -> dict[str, Any]:
            return {"metric": "ranking_control", "value": value, "basis": "ENGINE_COMPUTED"}

        lens("a-low", mtf(0.4), control(2))
        lens("b-off-grid", mtf(0.95, OTHER_GRID), control(3))
        lens("c-high", mtf(0.7, [0.0, 0.4, 0.8], weights=[3, 6]), control(1))
        lens("d-unsampled", mtf(0.9, None))
        lens("e-other-conditions", mtf(0.9, frequency=80))
        lens("f-other-orientation", mtf(0.9, orientation="tangential"))
        lens("g-two-grids", mtf(0.99, OTHER_GRID), mtf(0.8))
        lens("h-missing")
        interval = mtf(0.99, OTHER_GRID)
        interval.pop("value")
        interval.update(metric="ranking_interval_or_value", min_mtf=0.4, max_mtf=0.99)
        scalar = {**mtf(0.6), "metric": "ranking_interval_or_value"}
        lens("i-interval-and-scalar", interval, scalar)

        def zoom(record_id: str, grid: list[float]) -> None:
            def mutate(record: dict[str, Any]) -> None:
                record["record_schema_version"] = "1.1"
                conjugate = record["design"].pop("conjugate")
                record["design"]["configuration_count"] = 2
                sections = {name: record.pop(name) for name in ("first_order", "aperture", "field")}
                validity = record["design"]["aperture_validity"]
                validity["satisfies_stop_dependent_constraints"] = True
                record["configurations"] = [
                    {**sections, "configuration_id": "Wide", "conjugate": conjugate, "performance": [mtf(0.4)]},
                    {**sections, "configuration_id": "Tele", "conjugate": conjugate, "performance": [mtf(0.8, grid)]},
                ]
            project = helpers.minimal_project([{"configuration_id": "Wide"}, {"configuration_id": "Tele"}])
            corpus.add(record_id, project=project, with_evidence=False, mutate=mutate)

        zoom("zoom-comparable", GRID)
        zoom("zoom-mixed", OTHER_GRID)
        path = directory / "index.sqlite"
        build_index(corpus.root, path)
        cls.index = open_index(path)
        cls.addClassCleanup(cls.index.close)

    def search(self, record_ids: list[str], **request: Any) -> list[dict[str, Any]]:
        return query.search(self.index, {"mode": "starting_point", "filters": {"record_ids": record_ids},
                                         **request})["result"]["candidates"]

    def ranking_requests(self, **pins: Any) -> tuple[dict[str, Any], ...]:
        return ({"targets": [{"fact": MTF, "target": 0.8, **pins}]},
                {"preferences": [{"fact": MTF, "direction": "descending", **pins}]})

    def test_common_grid_and_proportional_weights_rank_implicitly(self) -> None:
        for request in self.ranking_requests():
            with self.subTest(request=request):
                candidates = self.search(["a-low", "c-high", "h-missing"], **request)
                self.assertEqual([c["record_id"] for c in candidates], ["c-high", "a-low", "h-missing"])
                self.assertEqual([c["unknowns"] for c in candidates[:2]], [[], []])
                self.assertEqual(candidates[2]["unknowns"][0]["reason"], "FACT_MISSING")

    def test_mixed_grids_surface_as_unknown_for_each_participant(self) -> None:
        cases = [(["a-low", "b-off-grid", "c-high"], [GRID, OTHER_GRID, GRID]),
                 (["a-low", "d-unsampled"], [GRID, None])]
        for record_ids, grids in cases:
            for request in self.ranking_requests():
                with self.subTest(record_ids=record_ids, request=request):
                    candidates = self.search(record_ids, **request)
                    self.assertEqual([c["record_id"] for c in candidates], record_ids)
                    for candidate, grid in zip(candidates, grids):
                        [unknown] = candidate["unknowns"]
                        self.assertEqual(unknown["reason"], "SAMPLING_UNSPECIFIED")
                        self.assertEqual(unknown["details"]["actual"], grid)
                        self.assertEqual(unknown["details"]["differs_by"], ["sampled_field_fractions"])
                        key = candidate["rank"]["key"]
                        if "targets" in request:
                            self.assertEqual(key[2:4], [1, 0])
                            self.assertEqual(candidate["shortfalls"], [])
                        else:
                            self.assertEqual(key[4], [1, None])

    def test_shared_grid_still_requires_comparable_conditions_and_qualifiers(self) -> None:
        cases = [("e-other-conditions", "conditions"), ("f-other-orientation", "qualifiers")]
        for record_id, differs in cases:
            for request in self.ranking_requests():
                with self.subTest(record_id=record_id, request=request):
                    candidates = self.search(["a-low", record_id], detail="full", **request)
                    self.assertEqual([c["record_id"] for c in candidates], ["a-low", record_id])
                    for candidate in candidates:
                        [unknown] = candidate["unknowns"]
                        self.assertEqual(unknown["reason"], "CONDITIONS_DIFFER")
                        self.assertEqual(unknown["details"]["differs_by"], [differs])
                        self.assertIn(differs, unknown["details"])

    def test_pin_to_common_grid_preserves_other_condition_checks(self) -> None:
        for request in self.ranking_requests(required_sampled_field_fractions=GRID):
            with self.subTest(request=request):
                candidates = self.search(["a-low", "c-high", "e-other-conditions"], **request)
                self.assertTrue(all(c["unknowns"][0]["reason"] == "CONDITIONS_DIFFER" for c in candidates))
                for item in request.values():
                    item[0]["conditions"] = {"frequency_lp_mm": 40}
                ranked = self.search(["a-low", "c-high", "e-other-conditions"], **request)
                self.assertEqual([c["record_id"] for c in ranked], ["c-high", "a-low", "e-other-conditions"])
                self.assertEqual([c["unknowns"] for c in ranked[:2]], [[], []])

    def test_grid_pin_selects_entry_before_ambiguity_check(self) -> None:
        record_ids = ["a-low", "b-off-grid", "c-high", "d-unsampled", "g-two-grids"]
        for request in self.ranking_requests(required_sampled_field_fractions=GRID):
            with self.subTest(request=request):
                candidates = self.search(record_ids, **request)
                self.assertEqual([c["record_id"] for c in candidates],
                                 ["g-two-grids", "c-high", "a-low", "b-off-grid", "d-unsampled"])
                self.assertEqual([c["unknowns"] for c in candidates[:3]], [[], [], []])
                self.assertEqual([c["unknowns"][0]["reason"] for c in candidates[3:]],
                                 ["SAMPLING_DIFFERS", "SAMPLING_DIFFERS"])

    def test_pin_to_scalar_skips_an_interval_on_a_different_grid(self) -> None:
        for request in self.ranking_requests(fact="performance.ranking_interval_or_value",
                                             required_sampled_field_fractions=GRID):
            with self.subTest(request=request):
                [candidate] = self.search(["i-interval-and-scalar"], **request)
                self.assertEqual(candidate["unknowns"], [])

    def test_mixed_grid_decision_precedes_configuration_choice(self) -> None:
        for request in self.ranking_requests():
            with self.subTest(request=request):
                [comparable] = self.search(["zoom-comparable"], **request)
                [mixed] = self.search(["zoom-mixed"], **request)
                self.assertEqual(comparable["configuration_id"], "Tele")
                self.assertEqual(mixed["configuration_id"], "Wide")
                self.assertEqual(mixed["unknowns"][0]["reason"], "SAMPLING_UNSPECIFIED")

    def test_mixed_grid_uses_other_preference_across_pages(self) -> None:
        request = {"mode": "starting_point", "limit": 1,
                   "filters": {"record_ids": ["a-low", "b-off-grid", "c-high"]},
                   "preferences": [{"fact": MTF, "direction": "descending"},
                                   {"fact": "performance.ranking_control", "direction": "ascending"}]}
        first = query.search(self.index, request)["result"]
        second = query.search(self.index, {**request, "cursor": first["page"]["next_cursor"]})["result"]
        self.assertEqual([page["candidates"][0]["record_id"] for page in (first, second)], ["c-high", "a-low"])
        for page, control in ((first, 1), (second, 2)):
            [candidate] = page["candidates"]
            self.assertEqual(candidate["rank"]["key"][4:6], [[1, None], [0, control]])

    def test_hard_excluded_grid_does_not_suppress_ranking(self) -> None:
        constraints = [{"fact": MTF, "op": "lte", "value": 0.7}]
        for request in self.ranking_requests():
            with self.subTest(request=request):
                candidates = self.search(["a-low", "c-high", "zoom-mixed"], constraints=constraints, **request)
                self.assertEqual([c["record_id"] for c in candidates], ["c-high", "a-low", "zoom-mixed"])
                self.assertEqual(candidates[-1]["configuration_id"], "Wide")
                self.assertTrue(all(not c["unknowns"] for c in candidates))

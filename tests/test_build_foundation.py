"""Foundation tests: canonical helpers, vocabulary, corpus validation, index build and store API."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from os_optics_lens import build as build_module  # noqa: E402
from os_optics_lens.build import build_index, dump_index, validate_corpus  # noqa: E402
from os_optics_lens.canonical import (  # noqa: E402
    JsonFormatError,
    canonical_json,
    canonical_key,
    corpus_relative_path,
    json_equal,
    loads_strict,
    record_hash,
    sha256_lf,
)
from os_optics_lens.corpus import load_corpus  # noqa: E402
from os_optics_lens.errors import LensError  # noqa: E402
from os_optics_lens.normalize import configuration_views, extract_facts, unit_for  # noqa: E402
from os_optics_lens.store import ENV_VAR, Index, resolve_source_in  # noqa: E402
from os_optics_lens.vocabulary import load_vocabulary  # noqa: E402

P2B = "yutong-2025-mirrorless-18-p2b"
SONNAR = "portrait-85-sonnar-eight"


def _reference_module():
    spec = importlib.util.spec_from_file_location("_reference_yutong", ROOT / "tests" / "test_yutong_acceptance.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Synthetic corpora ------------------------------------------------------------------------------


def _write_json(path: Path, data: Any, crlf: bool = False) -> bytes:
    text = json.dumps(data, indent=1, ensure_ascii=False) + "\n"
    payload = text.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload.replace(b"\n", b"\r\n") if crlf else payload)
    return payload


def minimal_project(configurations: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    project: dict[str, Any] = {"schema": "opensci.project", "project_schema_version": "1", "optical_models": []}
    if configurations is not None:
        project["configurations"] = configurations
    return project


def minimal_record(record_id: str, collection: str = "synth", evidence_path: str | None = None,
                   evidence_sha: str | None = None) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema": "opensci.lens.record",
        "record_schema_version": "1.0",
        "record_id": record_id,
        "record_revision": 1,
        "title": f"Synthetic {record_id}",
        "collection": {"collection_id": collection},
        "design": {
            "lens_type": "camera",
            "design_family_id": f"family-{record_id}",
            "design_scope": "COMPLETE_SYSTEM",
            "conjugate": "INFINITE_TO_FINITE",
            "configuration_count": 1,
            "aperture_validity": {"state": "STOP_BLOCKING", "stop_blocks_light": True,
                                  "eligible_for_stop_dependent_evaluation": True,
                                  "satisfies_stop_dependent_constraints": False},
        },
        "native_project": {"path": f"records/{collection}/{record_id}/project.json", "sha256": "0" * 64,
                           "engine_version": "0.14.0"},
        "spectrum": {
            "basis": "PROJECT_DECLARED",
            "sampling_wavelengths_nm": [486.1, 587.6, 656.3],
            "band_facets": ["visible"],
            "sampling_span_nm": {"min_nm": 486.1, "max_nm": 656.3, "eligible_for": "DISCOVERY_ONLY"},
            "continuous_coverage_nm": {"state": "UNCONFIRMED", "min_nm": None, "max_nm": None},
        },
        "first_order": {"effective_focal_length_mm": {"value": 50.0, "basis": "ENGINE_COMPUTED"}},
        "aperture": {"f_number": {"value": 2.8, "basis": "ENGINE_COMPUTED", "system_aperture": True}},
        "field": {"basis": "PROJECT_DECLARED", "kind": "ANGULAR", "half_field_deg": 23.0},
        "validation": {"paraxial": "MATCHED", "project_check": "ACCEPTED", "field": "NOT_CHECKED"},
        "record_hash_method": "SHA-256 over parsed JSON without record_hash and record_hash_method",
    }
    if evidence_path:
        record["performance"] = [{
            "metric": "mtf_field_minimum", "basis": "PROJECT_DERIVED", "value": 0.7,
            "conditions": {"frequency_lp_mm": 40},
            "source": {"path": evidence_path, "sha256": evidence_sha, "section": "mtf"},
        }]
    return record


class SynthCorpus:
    """Writes records with correct hashes into a temporary corpus root."""

    def __init__(self, root: Path) -> None:
        self.root = root
        (root / "records").mkdir(parents=True, exist_ok=True)
        (root / "LICENSE").write_text("MIT License\n\nCopyright (c) 2026 Synthetic\n", encoding="utf-8")

    def record_dir(self, collection: str, record_id: str) -> Path:
        return self.root / "records" / collection / record_id

    def add(self, record_id: str, collection: str = "synth", *, project: dict[str, Any] | None = None,
            with_evidence: bool = True, mutate=None, crlf: bool = False, record_dir: str | None = None,
            finalize_hash: bool = True) -> dict[str, Any]:
        """Write project, evidence and record; `mutate(record)` runs before the hash is computed."""
        directory = self.root / "records" / collection / (record_dir or record_id)
        project_bytes = _write_json(directory / "project.json", project or minimal_project(), crlf)
        evidence_path = evidence_sha = None
        if with_evidence:
            evidence = {"schema": "opensci.lens.evidence_extract", "sections": {"mtf": {"rows": [[0.0, 0.7]]}}}
            evidence_bytes = _write_json(directory / "evidence.json", evidence, crlf)
            evidence_path = f"records/{collection}/{record_dir or record_id}/evidence.json"
            evidence_sha = hashlib.sha256(evidence_bytes).hexdigest()
        record = minimal_record(record_id, collection, evidence_path, evidence_sha)
        record["native_project"]["path"] = f"records/{collection}/{record_dir or record_id}/project.json"
        record["native_project"]["sha256"] = hashlib.sha256(project_bytes).hexdigest()
        if mutate is not None:
            mutate(record)
        if finalize_hash:
            record["record_hash"] = record_hash(record)
        _write_json(directory / "record.json", record, crlf)
        return record

    def rewrite(self, collection: str, record_id: str, record: dict[str, Any], rehash: bool = True) -> None:
        if rehash:
            record["record_hash"] = record_hash(record)
        _write_json(self.record_dir(collection, record_id) / "record.json", record)


def codes(findings: list[Any]) -> set[tuple[str, str, str]]:
    return {(f.severity, f.code, f.field) for f in findings}


# Canonical helpers and vocabulary ---------------------------------------------------------------


class CanonicalTests(unittest.TestCase):
    def test_json_equal_semantics(self):
        self.assertTrue(json_equal(0, 0.0))
        self.assertTrue(json_equal([1, 2.0], [1.0, 2]))
        self.assertTrue(json_equal({"a": 1, "b": [0.5]}, {"b": [0.5], "a": 1.0}))
        self.assertFalse(json_equal(True, 1))
        self.assertFalse(json_equal(0, False))
        self.assertFalse(json_equal([1, 2], [2, 1]))
        self.assertFalse(json_equal({"a": 1}, {"a": 1, "b": None}))
        self.assertFalse(json_equal("1", 1))
        self.assertTrue(json_equal(None, None))
        self.assertEqual(canonical_key({"x": 1.0, "y": [2.0, 2.5]}), canonical_key({"y": [2, 2.5], "x": 1}))
        self.assertNotEqual(canonical_key(True), canonical_key(1))

    def test_canonical_json_and_hashes(self):
        self.assertEqual(canonical_json({"b": 1, "a": "é"}), '{"a":"é","b":1}')
        self.assertEqual(sha256_lf(b"a\r\nb\n"), hashlib.sha256(b"a\nb\n").hexdigest())
        record = json.loads((ROOT / "records/yutong-cup" / P2B / "record.json").read_text(encoding="utf-8"))
        self.assertEqual(record_hash(record), record["record_hash"])

    def test_strict_json_rejects_non_finite_with_pointer(self):
        with self.assertRaises(JsonFormatError) as ctx:
            loads_strict(b'{"a": {"b": [1, NaN]}}')
        self.assertEqual(ctx.exception.pointer, "/a/b/1")
        for text in (b'{"x": Infinity}', b'{"x": -Infinity}', b'{"x": 1e999}'):
            with self.assertRaises(JsonFormatError):
                loads_strict(text)
        with self.assertRaises(JsonFormatError):
            loads_strict(b"\xef\xbb\xbf{}")
        with self.assertRaises(JsonFormatError):
            loads_strict(b"{")
        self.assertEqual(loads_strict(b'{"x": 1.5}'), {"x": 1.5})

    def test_strict_json_rejects_out_of_range_input_with_pointer(self):
        # Parseable but unusable: each used to escape as OverflowError / UnicodeEncodeError /
        # RecursionError (an INTERNAL crash) instead of a MALFORMED_JSON finding.
        huge = "1" + "0" * 400
        cases = (
            ('{"a": {"b": [1, ' + huge + ']}}', "/a/b/1"),
            ('{"a": -' + huge + '}', "/a"),
            (r'{"title": "x\ud800y"}', "/title"),
            (r'{"k": {"\udc00": 1}}', "/k"),
            ('{"deep": ' + "[" * 200 + "]" * 200 + '}', "/deep" + "/0" * 127),
        )
        for text, pointer in cases:
            with self.subTest(pointer=pointer[:20]):
                with self.assertRaises(JsonFormatError) as ctx:
                    loads_strict(text.encode("ascii"))
                self.assertEqual(ctx.exception.pointer, pointer)
        # Still accepted: a large integer a double can hold, a valid surrogate pair, 128 levels.
        self.assertEqual(loads_strict(b'{"x": ' + b"9" * 300 + b"}")["x"], int("9" * 300))
        self.assertEqual(loads_strict(rb'{"x": "\ud83d\ude00"}'), {"x": "\U0001F600"})
        self.assertIsInstance(loads_strict(("[" * 128 + "]" * 128).encode()), list)
        with self.assertRaises(JsonFormatError):
            loads_strict(("[" * 129 + "]" * 129).encode())
        with self.assertRaises(JsonFormatError):
            loads_strict(("[" * 100000 + "]" * 100000).encode())

    def test_duplicate_keys_rejected_only_for_requests(self):
        # Record and evidence loading keep the default (last wins); request parsing opts in.
        self.assertEqual(loads_strict(b'{"a": 1, "a": 2}'), {"a": 2})
        for text, pointer in ((b'{"mode": "starting_point", "mode": "specification"}', "/mode"),
                              (b'{"q": [{"x": 1}, {"x": 1, "y": 2, "x": 3}]}', "/q/1/x"),
                              (b'{"a/b": {"k": 1, "k": 1}}', "/a~1b/k")):
            with self.subTest(pointer=pointer):
                with self.assertRaises(JsonFormatError) as ctx:
                    loads_strict(text, reject_duplicate_keys=True)
                self.assertEqual(ctx.exception.pointer, pointer)
                self.assertIn("duplicate key", ctx.exception.message)
        self.assertEqual(loads_strict(b'{"a": {"x": 1}, "b": {"x": 2}}', reject_duplicate_keys=True),
                         {"a": {"x": 1}, "b": {"x": 2}})

    def test_corpus_relative_path(self):
        self.assertEqual(corpus_relative_path("records/a/../a/b.json"), "records/a/b.json")
        for bad in ("", "/etc/passwd", "C:/x", "..", "../x", "records/../../x", "a\\b", None, 3):
            self.assertIsNone(corpus_relative_path(bad), bad)


class VocabularyTests(unittest.TestCase):
    def test_stop_dependence_lists(self):
        vocab = load_vocabulary()
        rules = vocab.stop_dependent_list()
        self.assertEqual(rules["sections"], ["aperture"])
        self.assertIn("aperture.stop_opening_diameter_mm", rules["stop_independent"])
        self.assertIn("aperture.property_list_comparison", rules["stop_independent"])
        for key in ("aperture.entrance_pupil_f_number", "aperture.working_f_number", "aperture.new_future_member",
                    "performance.mtf", "performance.mtf_field_minimum", "performance.relative_illumination"):
            self.assertTrue(vocab.is_stop_dependent(key), key)
        for key in ("aperture.stop_opening_diameter_mm", "aperture.property_list_comparison", "performance.cra",
                    "first_order.effective_focal_length_mm", "aperture"):
            self.assertFalse(vocab.is_stop_dependent(key), key)
        self.assertEqual(vocab.stop_dependent_prefix(["aperture", "working_f_number", "value"]),
                         "aperture.working_f_number")
        self.assertIsNone(vocab.stop_dependent_prefix(["aperture"]))

    def test_aliases_and_ambiguity(self):
        vocab = load_vocabulary()
        self.assertEqual(vocab.resolve_term("application", "Smartphone"), ("mobile_imaging", True))
        self.assertEqual(vocab.resolve_term("design_type", "photographic lens"), ("camera_lens", True))
        self.assertEqual(vocab.resolve_term("image_format", "FF"), ("full_frame", True))
        self.assertEqual(vocab.resolve_term("band", "NIR"), ("near_infrared", True))
        self.assertEqual(vocab.resolve_term("conjugate", "infinity"), ("infinite_to_finite", True))
        self.assertEqual(vocab.resolve_term("collection", "LensLibrary"), ("LensLibrary", False))
        with self.assertRaises(LensError) as ctx:
            vocab.resolve_term("design_type", "objective")
        self.assertEqual(ctx.exception.code, "AMBIGUOUS_TERM")
        self.assertEqual(ctx.exception.details["candidates"], ["endoscope_objective", "microscope_objective"])
        self.assertEqual(len(vocab.digest), 64)
        self.assertTrue(all(vocab.facet_description(name) for name in vocab.facet_names()))

    def test_units(self):
        self.assertEqual(unit_for("frequency_lp_mm"), "lp/mm")
        self.assertEqual(unit_for("paraxial_working_f_number_from_dump"), "1")
        self.assertEqual(unit_for("object_numerical_aperture"), "1")
        self.assertEqual(unit_for("pixel_pitch_um"), "um")
        self.assertEqual(unit_for("field_fraction"), "1")
        self.assertIsNone(unit_for("sagittal"))


# Real corpus -------------------------------------------------------------------------------------


class RealCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = load_corpus(ROOT)
        cls.records = {record.record_id: record for record in cls.corpus.records}

    def test_discovers_every_record_without_errors(self):
        on_disk = sorted(path.parent.name for path in (ROOT / "records").glob("*/*/record.json"))
        self.assertEqual(sorted(self.records), on_disk)
        self.assertGreaterEqual(len(on_disk), 41)
        self.assertEqual(self.corpus.errors, [])
        for rid in (P2B, SONNAR, "lenslibrary-528155"):
            self.assertIn(rid, self.records)
        self.assertEqual([r.record_id for r in self.corpus.records], sorted(self.records))
        self.assertIn("LICENSE", self.corpus.licenses)
        self.assertIn("licenses/LensLibrary-MIT.txt", self.corpus.licenses)

    def test_configuration_views_match_the_reference_evaluator(self):
        reference = _reference_module()
        case = reference.CASE
        record = self.records[P2B]
        self.assertEqual(len(record.views), 1)
        view = record.views[0]
        self.assertEqual((view.configuration_key, view.configuration_id, view.native, view.label, view.ordinal),
                         ("", None, False, "project as written", 0))
        self.assertEqual(view.view["configuration"], {"configuration_id": None, "label": "project as written",
                                                      "native": False})
        self.assertEqual(view.view["collection"], {"collection_id": "yutong-cup"})
        self.assertNotIn("validation", view.view)
        self.assertNotIn("engine_check", view.view["first_order"])
        for constraint in case["constraints"]:
            self.assertEqual(reference.evaluate(view.view, constraint), case["expected"]["outcome"], constraint["id"])

    def test_p2b_fact_samples(self):
        facts = self.records[P2B].facts[""]
        self.assertEqual(extract_facts(self.records[P2B].views[0].view), facts)
        by_key: dict[str, list[dict[str, Any]]] = {}
        for fact in facts:
            by_key.setdefault(fact["fact_key"], []).append(fact)
        efl = by_key["first_order.effective_focal_length_mm"][0]
        self.assertEqual((efl["json_path"], efl["unit"], efl["basis"], efl["basis_class"]),
                         ("first_order.effective_focal_length_mm.value", "mm", "ENGINE_COMPUTED", "ENGINE_COMPUTED"))
        self.assertAlmostEqual(efl["value_num"], 18.726068736671145)
        self.assertEqual(efl["conditions_text"], "Primary wavelength 587.0 nm (vacuum).")
        self.assertIsNone(efl["conditions_json"])
        self.assertEqual(json.loads(efl["constraint_form"]),
                         {"scope": ["first_order", "effective_focal_length_mm"], "metric": None, "path": "value"})
        mtf = by_key["performance.mtf_field_minimum"][0]
        self.assertEqual((mtf["json_path"], mtf["metric"], mtf["unit"]), ("performance[2].value", "mtf_field_minimum", "1"))
        self.assertEqual(json.loads(mtf["conditions_json"])["field_end"], 0.8)
        self.assertEqual(mtf["source_field"], "performance[2].source")
        self.assertEqual(len(by_key["performance.mtf"]), 2)
        point = by_key["performance.mtf"][0]
        self.assertIsNone(point["value_num"])
        self.assertEqual(json.loads(point["value_paths"]), ["sagittal", "tangential"])
        self.assertEqual(json.loads(point["qualifiers_json"]),
                         {"field_fraction": 0.8, "frequency_lp_mm": 40, "image_height_mm": 17.308})
        distortion = by_key["performance.distortion_range"][0]
        self.assertEqual((distortion["value_min"], distortion["value_max"], distortion["unit"]),
                         (-1.5167704304624996, 0.6591635163021525, "percent"))
        span = by_key["spectrum.sampling_span_nm"][0]
        self.assertEqual((span["value_min"], span["value_max"], span["eligible_for"], span["unit"]),
                         (486.0, 656.0, "DISCOVERY_ONLY", "nm"))
        coverage = by_key["spectrum.continuous_coverage_nm"][0]
        self.assertEqual((coverage["state"], coverage["value_min"], coverage["value_max"]), ("UNCONFIRMED", None, None))
        wavelengths = by_key["spectrum.sampling_wavelengths_nm"][0]
        self.assertEqual(json.loads(wavelengths["value_json"]), [486, 587, 656])
        self.assertEqual(wavelengths["basis"], "PROJECT_DECLARED")
        self.assertEqual(json.loads(by_key["image_format.cover_glass.material"][0]["value_json"]), "H-K9")
        self.assertEqual(by_key["image_format.cover_glass.thickness_mm"][0]["value_num"], 1.0)
        self.assertNotIn("image_format.pixel_pitch_um", by_key)
        stop = by_key["aperture.stop_opening_diameter_mm"][0]
        self.assertTrue(stop["system_aperture"])
        self.assertEqual(by_key["field.half_field_deg"][0]["value_num"], 49.555169168052586)
        self.assertIsNone(by_key["field.half_field_deg"][0]["state"])
        tolerance = by_key["analysis_evidence.tolerance"][0]
        self.assertEqual(json.loads(tolerance["value_json"])["mtf_gate"]["passed"], 92)
        self.assertEqual(tolerance["state"], "simulated")
        self.assertNotIn("first_order.engine_check", by_key)
        self.assertNotIn("aperture.property_list_comparison", by_key)

    def test_lenslibrary_fact_samples(self):
        facts = {f["fact_key"]: f for f in self.records["lenslibrary-6744570a"].facts[""]}
        system = facts["aperture.stop_opening_diameter_mm"]
        self.assertEqual((system["unit"], system["system_aperture"], system["basis_class"]),
                         ("mm", True, "PROJECT_DECLARED"))
        self.assertNotIn("aperture.paraxial_working_f_number", facts)
        self.assertNotIn("aperture.paraxial_working_f_number_from_dump", facts)
        self.assertNotIn("aperture.working_f_number", facts)
        self.assertNotIn("field.full_field_deg", facts)
        self.assertNotIn("field.field_points_deg", facts)
        self.assertEqual(facts["design.aperture_validity.stop_blocks_light"]["value_json"], "true")

    def test_facets(self):
        record = self.records["lenslibrary-6744570a"]
        values = {(v.facet, v.value, v.basis) for v in record.record_facets}
        self.assertIn(("design_type", "camera_lens", "VOCABULARY_MAPPING"), values)
        self.assertIn(("source_lens_type", "cell_phone", "RECORD"), values)
        self.assertIn(("application", "mobile_imaging", "RECORD"), values)
        self.assertIn(("function", "imaging", "VOCABULARY_MAPPING"), values)
        self.assertIn(("stop_state", "stop_blocking", "RECORD"), values)
        self.assertFalse(any(facet == "validation" for facet, _, _ in values))
        config = {(v.facet, v.value) for v in record.config_facets[""]}
        self.assertIn(("system_aperture", "stop_opening_diameter_mm"), config)
        self.assertIn(("conjugate", "infinite_to_finite"), config)
        self.assertIn(("band", "visible"), config)
        p2b = {(v.facet, v.value) for v in self.records[P2B].config_facets[""]}
        for expected in (("evidence", "mtf_field_minimum"), ("evidence", "tolerance_study"),
                         ("evidence", "ghost_study"), ("image_format", "full_frame")):
            self.assertIn(expected, p2b)


# Synthetic validation findings -----------------------------------------------------------------


class SyntheticFindingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "corpus"
        self.corpus = SynthCorpus(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def findings(self, allow: bool = False):
        return load_corpus(self.root, allow_unresolved_evidence=allow).findings

    def assert_finding(self, findings, severity, code, field, record_id=None, file=None):
        matches = [f for f in findings if (f.severity, f.code, f.field) == (severity, code, field)]
        self.assertTrue(matches, f"no {severity} {code} at {field!r}; got {[f.to_dict() for f in findings]}")
        if record_id is not None:
            self.assertIn(record_id, {f.record_id for f in matches})
        if file is not None:
            self.assertIn(file, {f.file for f in matches})
        return matches

    def test_clean_synthetic_corpus_with_crlf_files(self):
        self.corpus.add("rec-a", crlf=True)
        self.corpus.add("rec-b")
        corpus = load_corpus(self.root)
        self.assertEqual([f.to_dict() for f in corpus.findings], [])
        self.assertEqual([r.record_id for r in corpus.records], ["rec-a", "rec-b"])
        self.assertEqual(list(corpus.evidence_files), ["records/synth/rec-a/evidence.json",
                                                        "records/synth/rec-b/evidence.json"])
        content = corpus.evidence_files["records/synth/rec-a/evidence.json"][1]
        self.assertNotIn(b"\r\n", content)

    def test_malformed_json_and_nan(self):
        self.corpus.add("rec-a")
        broken = self.corpus.record_dir("synth", "rec-a") / "record.json"
        broken.write_text('{"schema": "opensci.lens.record",', encoding="utf-8")
        self.corpus.add("rec-b")
        nan_file = self.corpus.record_dir("synth", "rec-b") / "record.json"
        nan_file.write_text(nan_file.read_text(encoding="utf-8").replace('"value": 50.0', '"value": NaN'),
                            encoding="utf-8")
        findings = self.findings()
        self.assert_finding(findings, "error", "MALFORMED_JSON", "", file="records/synth/rec-a/record.json")
        self.assert_finding(findings, "error", "MALFORMED_JSON", "first_order.effective_focal_length_mm.value",
                            file="records/synth/rec-b/record.json")

    def test_out_of_range_values_fail_the_build_with_findings(self):
        # Values that parse but cannot be indexed are findings naming file and field (BUILD_FAILED,
        # exit 6), never an INTERNAL crash.
        huge = "1" + "0" * 400
        edits = {
            "rec-int": ('"configuration_count": 1', '"configuration_count": ' + huge,
                        "MALFORMED_JSON", "design.configuration_count"),
            "rec-value": ('"value": 50.0', '"value": ' + huge,
                          "MALFORMED_JSON", "first_order.effective_focal_length_mm.value"),
            "rec-surrogate": ('"title": "Synthetic', '"title": "\\ud800Synthetic', "MALFORMED_JSON",
                              "title"),
            "rec-deep": ('"collection": {', '"collection": {"deep": ' + "[" * 900 + "]" * 900 + ", ",
                         "MALFORMED_JSON", "collection.deep" + "[0]" * 126),
        }
        for record_id, (old, new, _, _) in edits.items():
            self.corpus.add(record_id)
            path = self.corpus.record_dir("synth", record_id) / "record.json"
            text = path.read_text(encoding="utf-8")
            self.assertIn(old, text)
            path.write_text(text.replace(old, new, 1), encoding="utf-8")
        self.corpus.add("rec-revision", mutate=lambda record: record.__setitem__("record_revision", 2 ** 63))
        self.corpus.add("rec-ok")
        with self.assertRaises(LensError) as ctx:
            build_index(self.root, Path(self._tmp.name) / "out.sqlite")
        error = ctx.exception
        self.assertEqual((error.code, error.exit_code), ("BUILD_FAILED", 6))
        found = {(f["record_id"] or f["file"].split("/")[2], f["code"], f["field"])
                 for f in error.details["findings"]}
        for record_id, (_, _, code, field) in edits.items():
            self.assertIn((record_id, code, field), found)
        self.assertIn(("rec-revision", "INVALID_FIELD", "record_revision"), found)
        self.assertEqual(error.details["errors"], 5)

    def test_missing_and_invalid_fields(self):
        def mutate(record):
            del record["title"]
            record["record_revision"] = True
            record["design"].pop("design_family_id")
            record["first_order"]["effective_focal_length_mm"]["value"] = "fifty"
            record["record_schema_version"] = "9.9"

        self.corpus.add("rec-a", mutate=mutate)
        findings = self.findings()
        self.assert_finding(findings, "error", "MISSING_FIELD", "title", "rec-a")
        self.assert_finding(findings, "error", "INVALID_FIELD", "record_revision", "rec-a")
        self.assert_finding(findings, "warning", "MISSING_FAMILY", "design.design_family_id", "rec-a")
        self.assert_finding(findings, "error", "INVALID_FIELD", "first_order.effective_focal_length_mm.value")
        self.assert_finding(findings, "error", "UNSUPPORTED_SCHEMA_VERSION", "record_schema_version")
        self.assertEqual(findings, sorted(findings, key=lambda f: (f.file, f.field, f.code, f.record_id or "",
                                                                   f.message)))

    def test_duplicate_ids_and_directory_mismatch(self):
        self.corpus.add("rec-a")
        self.corpus.add("rec-a", record_dir="rec-a-copy")
        corpus = load_corpus(self.root)
        dupes = self.assert_finding(corpus.findings, "error", "DUPLICATE_RECORD_ID", "record_id", "rec-a")
        self.assertEqual({f.file for f in dupes}, {"records/synth/rec-a/record.json",
                                                   "records/synth/rec-a-copy/record.json"})
        self.assert_finding(corpus.findings, "error", "RECORD_DIR_MISMATCH", "record_id",
                            file="records/synth/rec-a-copy/record.json")
        self.assertEqual(corpus.records, [])

    def test_hash_mismatches(self):
        record = self.corpus.add("rec-a")
        record["title"] = "edited without rehash"
        self.corpus.rewrite("synth", "rec-a", record, rehash=False)
        project = self.corpus.record_dir("synth", "rec-a") / "project.json"
        project.write_bytes(project.read_bytes() + b"\n")
        evidence = self.corpus.record_dir("synth", "rec-a") / "evidence.json"
        evidence.write_bytes(evidence.read_bytes() + b" ")
        findings = self.findings()
        [mismatch] = self.assert_finding(findings, "error", "RECORD_HASH_MISMATCH", "record_hash", "rec-a")
        self.assertEqual(mismatch.actual, record["record_hash"])
        self.assertEqual(mismatch.expected, record_hash(record))
        self.assert_finding(findings, "error", "PROJECT_HASH_MISMATCH", "native_project.sha256", "rec-a")
        [evidence_mismatch] = self.assert_finding(findings, "error", "EVIDENCE_HASH_MISMATCH",
                                                  "performance[0].source.sha256", "rec-a")
        self.assertEqual(evidence_mismatch.expected, sha256_lf(evidence.read_bytes()))

    def test_broken_project_references(self):
        self.corpus.add("rec-a", mutate=lambda r: r["native_project"].update(path="records/synth/rec-a/none.json"))
        self.corpus.add("rec-b", mutate=lambda r: r["native_project"].update(path="../outside/project.json"))
        self.corpus.add("rec-c", project={"schema": "something.else"})
        findings = self.findings()
        self.assert_finding(findings, "error", "PROJECT_MISSING", "native_project.path", "rec-a")
        self.assert_finding(findings, "error", "PROJECT_PATH_ESCAPES", "native_project.path", "rec-b")
        self.assert_finding(findings, "error", "PROJECT_MALFORMED", "native_project.path", "rec-c")

    def test_broken_evidence_references_and_allow_mode(self):
        def missing_file(record):
            record["performance"][0]["source"]["path"] = "records/synth/rec-a/missing.json"

        def missing_section(record):
            record["performance"][0]["source"]["section"] = "nope"

        self.corpus.add("rec-a", mutate=missing_file)
        record_b = self.corpus.add("rec-b", mutate=missing_section)
        self.corpus.add("rec-c")
        malformed = self.corpus.record_dir("synth", "rec-c") / "evidence.json"
        malformed.write_text('{"sections": []}\n', encoding="utf-8")
        record_c = json.loads((self.corpus.record_dir("synth", "rec-c") / "record.json").read_text(encoding="utf-8"))
        record_c["performance"][0]["source"]["sha256"] = sha256_lf(malformed.read_bytes())
        self.corpus.rewrite("synth", "rec-c", record_c)

        strict = self.findings()
        self.assert_finding(strict, "error", "EVIDENCE_FILE_MISSING", "performance[0].source.path", "rec-a")
        self.assert_finding(strict, "error", "EVIDENCE_SECTION_MISSING", "performance[0].source.section", "rec-b")
        self.assert_finding(strict, "error", "EVIDENCE_MALFORMED", "performance[0].source.path", "rec-c")

        out = Path(self._tmp.name) / "index.sqlite"
        with self.assertRaises(LensError) as ctx:
            build_index(self.root, out)
        self.assertEqual(ctx.exception.code, "BUILD_FAILED")
        self.assertEqual(ctx.exception.exit_code, 6)
        self.assertIn("--allow-unresolved-evidence", ctx.exception.hint)
        self.assertFalse(out.exists())

        result = build_index(self.root, out, allow_unresolved_evidence=True)
        self.assertEqual(result.records, 3)
        self.assertEqual(result.warnings, 3)
        with Index.open(out) as index:
            self.assertEqual(index.meta()["allow_unresolved_evidence"], "1")
            statuses = {}
            for row in index.iter_records():
                for ref in index.evidence_refs(row["record_pk"]):
                    if ref["field"] == "performance[0].source":
                        statuses[row["record_id"]] = ref["status"]
            self.assertEqual(statuses, {"rec-a": "FILE_MISSING", "rec-b": "SECTION_MISSING", "rec-c": "MALFORMED"})
            self.assertEqual(index.evidence_paths(), ["records/synth/rec-b/evidence.json"])
            self.assertNotIn("performance", index.get_record("rec-b")["record"])
            self.assertEqual(index.resolve_source(record_b["performance"][0]["source"])["status"], "SECTION_MISSING")
            self.assertEqual({f["code"] for f in index.findings()},
                             {"EVIDENCE_FILE_MISSING", "EVIDENCE_SECTION_MISSING", "EVIDENCE_MALFORMED"})

    def test_source_ref_missing_a_key_is_malformed(self):
        # A `source` object missing `section` (or `sha256`) is still a reference: it is reported,
        # not skipped, and recorded as MALFORMED under --allow-unresolved-evidence.
        def no_section(record):
            del record["performance"][0]["source"]["section"]

        def no_sha(record):
            del record["performance"][0]["source"]["sha256"]

        self.corpus.add("rec-a", mutate=no_section)
        self.corpus.add("rec-b", mutate=no_sha)
        self.corpus.add("rec-c")
        strict = self.findings()
        self.assert_finding(strict, "error", "EVIDENCE_MALFORMED", "performance[0].source.section", "rec-a")
        self.assert_finding(strict, "error", "EVIDENCE_MALFORMED", "performance[0].source.sha256", "rec-b")
        self.assertNotIn("rec-c", {f.record_id for f in strict})  # native_project is not a source
        out = Path(self._tmp.name) / "index.sqlite"
        with self.assertRaises(LensError) as ctx:
            build_index(self.root, out)
        self.assertEqual(ctx.exception.code, "BUILD_FAILED")
        allowed = self.findings(allow=True)
        self.assert_finding(allowed, "warning", "EVIDENCE_MALFORMED", "performance[0].source.section", "rec-a")
        build_index(self.root, out, allow_unresolved_evidence=True)
        with Index.open(out) as index:
            refs = [ref for row in index.iter_records() if row["record_id"] == "rec-a"
                    for ref in index.evidence_refs(row["record_pk"]) if ref["field"] == "performance[0].source"]
            self.assertEqual([ref["status"] for ref in refs], ["MALFORMED"])

    def test_license_notice_path_not_a_string_is_missing_not_escape(self):
        for index, bad in enumerate((None, 5, "")):
            self.corpus.add(f"rec-{index}", mutate=lambda r, bad=bad: r["collection"].update(
                license={"spdx": "MIT", "notice_path": bad}))
        strict = self.findings()
        self.assertNotIn("PROJECT_PATH_ESCAPES", {f.code for f in strict})
        for index in range(3):
            self.assert_finding(strict, "error", "LICENSE_NOTICE_MISSING", "collection.license.notice_path",
                                f"rec-{index}")
        allowed = self.findings(allow=True)
        self.assertEqual({(f.severity, f.code) for f in allowed}, {("warning", "LICENSE_NOTICE_MISSING")})
        result = build_index(self.root, Path(self._tmp.name) / "index.sqlite", allow_unresolved_evidence=True)
        self.assertEqual(result.records, 3)
        # An absolute path is still an escape, never downgraded.
        self.corpus.add("rec-abs", mutate=lambda r: r["collection"].update(license={"notice_path": "/etc/x"}))
        self.assert_finding(self.findings(allow=True), "error", "PROJECT_PATH_ESCAPES",
                            "collection.license.notice_path", "rec-abs")

    def test_license_notice(self):
        def notice(record):
            record["collection"]["license"] = {"spdx": "MIT", "notice_path": "LICENSE",
                                               "copyright": "Copyright (c) 2026 Someone Else"}

        self.corpus.add("rec-a", mutate=notice)
        self.corpus.add("rec-b", mutate=lambda r: r["collection"].update(
            license={"notice_path": "licenses/NOPE.txt", "copyright": "x"}))
        findings = self.findings()
        self.assert_finding(findings, "error", "LICENSE_NOTICE_MISMATCH", "collection.license.copyright", "rec-a")
        self.assert_finding(findings, "error", "LICENSE_NOTICE_MISSING", "collection.license.notice_path", "rec-b")

    def test_configurations(self):
        project = minimal_project([{"configuration_id": "Wide", "label": "wide end"},
                                   {"configuration_id": "Tele"}])

        def two_configurations(record):
            record["record_schema_version"] = "1.1"
            record["design"].pop("conjugate")
            record["design"]["configuration_count"] = 2
            first_order, aperture, field = record.pop("first_order"), record.pop("aperture"), record.pop("field")
            performance = record.pop("performance")
            wide = {"configuration_id": "Wide", "conjugate": "INFINITE_TO_FINITE", "first_order": first_order,
                    "aperture": aperture, "field": field, "performance": performance}
            tele = copy.deepcopy(wide)
            tele.update(configuration_id="Tele", label="tele end")
            tele["first_order"]["effective_focal_length_mm"]["value"] = 100.0
            record["configurations"] = [wide, tele]

        record = self.corpus.add("zoom", project=project, mutate=two_configurations)
        corpus = load_corpus(self.root)
        self.assertEqual([f.to_dict() for f in corpus.findings], [])
        [normalized] = corpus.records
        views = normalized.views
        self.assertEqual([(v.configuration_key, v.configuration_id, v.native, v.label, v.ordinal) for v in views],
                         [("Wide", "Wide", True, "wide end", 0), ("Tele", "Tele", True, "tele end", 1)])
        self.assertNotIn("configurations", views[0].view)
        self.assertEqual(views[1].view["first_order"]["effective_focal_length_mm"]["value"], 100.0)
        self.assertEqual(views[0].view["design"]["conjugate"], "INFINITE_TO_FINITE")
        efl = {key: [f["value_num"] for f in facts if f["fact_key"] == "first_order.effective_focal_length_mm"]
               for key, facts in normalized.facts.items()}
        self.assertEqual(efl, {"Wide": [50.0], "Tele": [100.0]})
        self.assertEqual(configuration_views(record, project)[1].configuration_id, "Tele")

        out = Path(self._tmp.name) / "index.sqlite"
        result = build_index(self.root, out)
        self.assertEqual((result.records, result.configurations), (1, 2))
        with Index.open(out) as index:
            rec = index.get_record("zoom")
            configs = index.configurations(rec["record_pk"])
            self.assertEqual([c["configuration_id"] for c in configs], ["Wide", "Tele"])
            self.assertEqual(index.configuration(rec["record_pk"], "Tele")["label"], "tele end")
            with self.assertRaises(LensError) as ctx:
                index.configuration(rec["record_pk"], "Mid")
            self.assertEqual(ctx.exception.code, "CONFIGURATION_NOT_FOUND")
            self.assertEqual(index.project(rec["record_pk"])["native_configuration_ids"], ["Wide", "Tele"])
            facts = index.facts(record_pk=rec["record_pk"], fact_key="performance.mtf_field_minimum")
            self.assertEqual(len(facts), 2)
            refs = {ref["ref_pk"]: ref["field"] for ref in index.evidence_refs(rec["record_pk"])}
            self.assertEqual(sorted(refs[f["source_ref_pk"]] for f in facts),
                             ["configurations[0].performance[0].source", "configurations[1].performance[0].source"])

    def test_configuration_errors(self):
        self.corpus.add("unmapped", project=minimal_project([{"configuration_id": "A"}, {"configuration_id": "B"}]))
        self.corpus.add("single", project=minimal_project([{"configuration_id": "Only", "label": "the one"}]))
        self.corpus.add("invalid", project=minimal_project([{"configuration_id": "con"}, {"configuration_id": "X"},
                                                            {"configuration_id": "x"}, {"configuration_id": "9a"}]))
        self.corpus.add("old", mutate=lambda r: r.update(configurations=[{"configuration_id": "A"}]))
        self.corpus.add("count", mutate=lambda r: r["design"].update(configuration_count=2))

        def unknown(record):
            record["record_schema_version"] = "1.1"
            record["design"].pop("conjugate")
            record["configurations"] = [
                {"configuration_id": "Ghost", "first_order": {}, "aperture": {}, "field": {}, "performance": [],
                 "conjugate": "INFINITE_TO_FINITE", "zoom_ratio": 2},
                {"configuration_id": "ghost", "first_order": {}, "aperture": {}, "field": {}, "performance": [],
                 "conjugate": "INFINITE_TO_FINITE"},
            ]

        def inherits(record):
            record["record_schema_version"] = "1.1"
            record["design"]["configuration_count"] = 1
            record["configurations"] = [{"configuration_id": "A"}]

        self.corpus.add("unknown", mutate=unknown)
        self.corpus.add("inherits", project=minimal_project([{"configuration_id": "A"}]), mutate=inherits)
        corpus = load_corpus(self.root)
        findings = corpus.findings
        self.assert_finding(findings, "error", "CONFIGURATION_UNMAPPED", "configurations", "unmapped")
        self.assert_finding(findings, "error", "INVALID_FIELD", "configurations", "old")
        self.assert_finding(findings, "error", "CONFIGURATION_COUNT_MISMATCH", "design.configuration_count", "count")
        invalid = self.assert_finding(findings, "error", "PROJECT_CONFIGURATION_INVALID",
                                      "configurations[0].configuration_id", "invalid")
        self.assertEqual(invalid[0].file, "records/synth/invalid/project.json")
        self.assert_finding(findings, "error", "PROJECT_CONFIGURATION_INVALID", "configurations[2].configuration_id")
        self.assert_finding(findings, "error", "PROJECT_CONFIGURATION_INVALID", "configurations[3].configuration_id")
        self.assert_finding(findings, "error", "CONFIGURATION_UNKNOWN", "configurations[0].configuration_id", "unknown")
        self.assert_finding(findings, "error", "DUPLICATE_CONFIGURATION_ID", "configurations[1].configuration_id")
        self.assert_finding(findings, "error", "INVALID_FIELD", "configurations[0].zoom_ratio", "unknown")
        inherited = [f for f in findings if f.record_id == "inherits" and f.code == "INVALID_FIELD"]
        self.assertEqual({f.field for f in inherited}, {"configurations"})
        self.assertEqual(len(inherited), 5)
        self.assertNotIn("single", {f.record_id for f in findings})
        single = next(r for r in corpus.records if r.record_id == "single")
        self.assertEqual((single.views[0].configuration_id, single.views[0].native, single.views[0].label),
                         ("Only", True, "the one"))

    def test_batch_manifest_and_vocabulary_warnings(self):
        self.corpus.add("rec-a", mutate=lambda r: r["design"].update(lens_type="kaleidoscope",
                                                                     structure_tags=["Fancy Layout"]))
        self.corpus.add("rec-b")
        manifest = {"collection": {"collection_id": "synth"}, "accepted_record_ids": ["rec-a", "rec-gone"]}
        _write_json(self.root / "records/synth/batch-synth-1.json", manifest)
        corpus = load_corpus(self.root)
        findings = corpus.findings
        self.assert_finding(findings, "error", "BATCH_MANIFEST_MISMATCH", "accepted_record_ids[1]", "rec-gone",
                            file="records/synth/batch-synth-1.json")
        self.assert_finding(findings, "warning", "BATCH_MANIFEST_MISMATCH", "record_id", "rec-b")
        self.assert_finding(findings, "warning", "UNKNOWN_TERM", "design.lens_type", "rec-a")
        self.assert_finding(findings, "warning", "UNKNOWN_TERM", "design.structure_tags[0]", "rec-a")
        rec_a = next(r for r in corpus.records if r.record_id == "rec-a")
        values = {(v.facet, v.value) for v in rec_a.record_facets}
        self.assertIn(("source_lens_type", "kaleidoscope"), values)
        self.assertIn(("structure", "fancy_layout"), values)
        self.assertNotIn("design_type", {v.facet for v in rec_a.record_facets})


# Build mechanics -------------------------------------------------------------------------------


class BuildMechanicsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "corpus"
        self.corpus = SynthCorpus(self.root)
        self.corpus.add("rec-a")
        self.corpus.add("rec-b", crlf=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_failed_build_keeps_previous_index_byte_identical(self):
        out = self.tmp / "out" / "index.sqlite"
        first = build_index(self.root, out)
        before = out.read_bytes()
        broken = self.corpus.record_dir("synth", "rec-a") / "record.json"
        broken.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(LensError) as ctx:
            build_index(self.root, out)
        error = ctx.exception
        self.assertEqual(error.code, "BUILD_FAILED")
        self.assertEqual(error.details["errors"], 1)
        self.assertEqual(error.details["findings"][0]["code"], "MALFORMED_JSON")
        self.assertEqual(error.details["findings"][0]["file"], "records/synth/rec-a/record.json")
        self.assertEqual(out.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in out.parent.iterdir()), ["index.sqlite"])
        with Index.open(out) as index:
            self.assertEqual(index.snapshot_id, first.snapshot_id)

    def test_index_in_use_keeps_previous_index(self):
        out = self.tmp / "index.sqlite"
        build_index(self.root, out)
        before = out.read_bytes()
        self.corpus.add("rec-c")
        with mock.patch.object(build_module.os, "replace", side_effect=PermissionError(13, "in use")):
            with self.assertRaises(LensError) as ctx:
                build_index(self.root, out)
        self.assertEqual((ctx.exception.code, ctx.exception.exit_code), ("INDEX_IN_USE", 5))
        self.assertEqual(out.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["corpus", "index.sqlite"])

    @unittest.skipUnless(os.name == "nt", "an open reader blocks os.replace only on Windows")
    def test_open_reader_blocks_replacement_on_windows(self):
        out = self.tmp / "index.sqlite"
        build_index(self.root, out)
        before = out.read_bytes()
        with Index.open(out) as index:
            index.meta()
            with self.assertRaises(LensError) as ctx:
                build_index(self.root, out)
        self.assertEqual(ctx.exception.code, "INDEX_IN_USE")
        self.assertEqual(out.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["corpus", "index.sqlite"])

    def test_reproducible_bytes_dump_and_snapshot(self):
        first, second = self.tmp / "a.sqlite", self.tmp / "b" / "b.sqlite"
        result_a = build_index(self.root, first)
        result_b = build_index(self.root, second)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(result_a.snapshot_id, result_b.snapshot_id)
        self.assertRegex(result_a.snapshot_id, r"^[0-9a-f]{16}$")
        dump = dump_index(first)
        self.assertEqual(canonical_json(dump), canonical_json(dump_index(second)))
        without_meta = {name: table for name, table in dump["tables"].items() if name != "meta"}
        meta = dict(dump["tables"]["meta"]["rows"])
        self.assertEqual(meta["content_digest"], hashlib.sha256(canonical_json(without_meta).encode()).hexdigest())
        project_row = dump["tables"]["projects"]["rows"][0]
        self.assertEqual(set(project_row[2]), {"sha256", "length"})
        self.assertEqual(validate_corpus(self.root).snapshot_id, result_a.snapshot_id)
        self.assertIsNone(validate_corpus(self.root).out)

        allow = build_index(self.root, self.tmp / "c.sqlite", allow_unresolved_evidence=True)
        self.assertNotEqual(allow.snapshot_id, result_a.snapshot_id)
        self.corpus.add("rec-c")
        grown = build_index(self.root, first)
        self.assertNotEqual(grown.snapshot_id, result_a.snapshot_id)
        self.assertEqual(grown.records, 3)

    def test_out_directory_or_read_only_file_is_not_index_in_use(self):
        adir = self.tmp / "adir"
        adir.mkdir()
        with mock.patch.object(build_module, "load_corpus") as loader:
            with self.assertRaises(LensError) as ctx:
                build_index(self.root, adir)
        self.assertEqual(ctx.exception.code, "INVALID_REQUEST")
        self.assertIn(str(adir.resolve()), ctx.exception.message)
        loader.assert_not_called()  # rejected before the corpus is read
        self.assertEqual(list(adir.iterdir()), [])

        out = self.tmp / "ro.sqlite"
        build_index(self.root, out)
        before = out.read_bytes()
        with mock.patch.object(build_module.os, "replace", side_effect=PermissionError(13, "Access is denied")),                 mock.patch.object(build_module.os, "access", return_value=False):
            with self.assertRaises(LensError) as ctx:
                build_index(self.root, out)
        self.assertEqual((ctx.exception.code, ctx.exception.details["out"]), ("INVALID_REQUEST", str(out.resolve())))
        self.assertEqual(out.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["adir", "corpus", "ro.sqlite"])

    @unittest.skipUnless(os.name == "nt", "os.replace onto a read-only file fails only on Windows")
    def test_read_only_out_on_windows(self):
        out = self.tmp / "ro.sqlite"
        build_index(self.root, out)
        before = out.read_bytes()
        os.chmod(out, 0o444)
        try:
            with self.assertRaises(LensError) as ctx:
                build_index(self.root, out)
        finally:
            os.chmod(out, 0o666)
        self.assertEqual(ctx.exception.code, "INVALID_REQUEST")
        self.assertIn("read-only", ctx.exception.message)
        self.assertEqual(out.read_bytes(), before)

    def test_missing_records_directory(self):
        with self.assertRaises(LensError) as ctx:
            build_index(self.tmp / "nowhere", self.tmp / "x.sqlite")
        self.assertEqual(ctx.exception.code, "INVALID_REQUEST")


# Store API on the real corpus -----------------------------------------------------------------


class StoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.path = Path(cls._tmp.name) / "index.sqlite"
        cls.result = build_index(ROOT, cls.path)
        cls.index = Index.open(cls.path)

    @classmethod
    def tearDownClass(cls):
        cls.index.close()
        cls._tmp.cleanup()

    def test_build_result_and_meta(self):
        on_disk = len(list((ROOT / "records").glob("*/*/record.json")))
        self.assertEqual((self.result.records, self.result.configurations), (on_disk, on_disk))
        self.assertEqual(self.result.warnings, 0)
        meta = self.index.meta()
        self.assertEqual(meta["snapshot_id"], self.result.snapshot_id)
        self.assertEqual(meta["index_schema_version"], "2")
        self.assertEqual(meta["builder"], "os-optics-lens 0.1.0")
        self.assertEqual(int(meta["fact_count"]), self.result.facts)
        self.assertEqual((self.index.record_count, self.index.configuration_count), (on_disk, on_disk))
        self.assertEqual(self.index.path, self.path.resolve())
        self.assertEqual(self.index.source, "argument")

    def test_records_and_lookup_errors(self):
        rows = list(self.index.iter_records())
        self.assertEqual([r["record_id"] for r in rows], sorted(r["record_id"] for r in rows))
        record = self.index.get_record(P2B)
        self.assertEqual((record["revision"], record["record"]["record_id"]), (4, P2B))
        with self.assertRaises(LensError) as ctx:
            self.index.get_record("yutong-2025-mirrorless-18")
        self.assertEqual((ctx.exception.code, ctx.exception.exit_code), ("RECORD_NOT_FOUND", 3))
        self.assertIn(P2B, ctx.exception.details["did_you_mean"])
        with self.assertRaises(LensError) as ctx:
            self.index.get_record(P2B, revision=3)
        self.assertEqual(ctx.exception.code, "REVISION_NOT_AVAILABLE")
        self.assertEqual(ctx.exception.details["available_revision"], 4)

    def test_configurations_facts_and_facets(self):
        record = self.index.get_record(P2B)
        [config] = self.index.configurations(record["record_pk"])
        self.assertEqual((config["configuration_id"], config["native"], config["configuration_key"]), (None, False, ""))
        self.assertEqual(config["view"]["record_id"], P2B)
        configs = list(self.index.iter_configurations(with_view=False))
        self.assertEqual(len(configs), self.index.configuration_count)
        self.assertNotIn("view", configs[0])
        efl = self.index.facts(fact_key="first_order.effective_focal_length_mm")
        self.assertEqual(len(efl), self.index.configuration_count)
        p2b_facts = self.index.facts(record_pk=record["record_pk"], key_prefix="performance.")
        self.assertEqual({f["fact_key"] for f in p2b_facts},
                         {"performance.mtf", "performance.mtf_field_minimum", "performance.cra",
                          "performance.relative_illumination", "performance.distortion_range"})
        self.assertIn("spectrum.sampling_span_nm", self.index.fact_keys())
        stop = self.index.facts(record_pk=record["record_pk"], fact_key="aperture.stop_opening_diameter_mm")[0]
        self.assertIs(stop["system_aperture"], True)
        rows = self.index.facet_rows(facet="design_type", values=["camera_lens"])
        self.assertTrue(rows)
        self.assertTrue(all(r["config_pk"] is None for r in rows))
        band = self.index.facet_rows(facet="band")
        self.assertTrue(all(r["config_pk"] is not None for r in band))
        self.assertEqual(self.index.facet_values("band"), ["visible"])
        self.assertIn("lenslibrary", self.index.facet_values("collection"))

    def test_evidence_resolution(self):
        record = self.index.get_record(P2B)
        view = self.index.configurations(record["record_pk"])[0]["view"]
        source = next(p for p in view["performance"] if p["metric"] == "relative_illumination")["source"]
        resolved = self.index.resolve_source(source)
        self.assertEqual(resolved["status"], "RESOLVED")
        self.assertEqual(resolved["section_value"]["relative_illumination_conditions"]["field_fraction"], 1.0)
        reference = _reference_module()
        self.assertEqual(resolved["section_value"], reference.evidence_section(source))
        fact = self.index.facts(record_pk=record["record_pk"], fact_key="performance.relative_illumination")[0]
        ref = self.index.evidence_ref(fact["source_ref_pk"])
        self.assertEqual((ref["field"], ref["status"]), ("performance[4].source", "RESOLVED"))
        content = self.index.evidence_file(source["path"], source["sha256"])
        self.assertEqual(hashlib.sha256(content).hexdigest(), source["sha256"])
        self.assertIsNone(self.index.evidence_file(source["path"], "0" * 64))
        cases = {
            "MISSING": None,
            "MALFORMED": dict(source, sha256="xyz"),
            "PATH_ESCAPES": dict(source, path="../evidence.json"),
            "FILE_MISSING": dict(source, path="records/yutong-cup/missing-evidence.json"),
            "HASH_MISMATCH": dict(source, sha256="0" * 64),
            "SECTION_MISSING": dict(source, section="no_such_section"),
        }
        for status, candidate in cases.items():
            self.assertEqual(self.index.resolve_source(candidate)["status"], status, status)
            self.assertIsNone(reference.evidence_section(candidate) if candidate else None)
        in_memory = {source["path"]: (source["sha256"], content)}
        self.assertEqual(resolve_source_in(in_memory, source)["status"], "RESOLVED")
        self.assertEqual(resolve_source_in(lambda path: None, source)["status"], "FILE_MISSING")

    def test_projects_and_licenses(self):
        for row in self.index.iter_records(with_json=True):
            content = self.index.project_bytes(row["record_pk"])
            digest = hashlib.sha256(content).hexdigest()
            self.assertEqual(digest, row["record"]["native_project"]["sha256"], row["record_id"])
            meta = self.index.project(row["record_pk"])
            self.assertEqual((meta["sha256"], meta["size_bytes"]), (digest, len(content)))
        licenses = self.index.licenses()
        self.assertIn("LICENSE", licenses)
        self.assertIn("licenses/LensLibrary-MIT.txt", licenses)
        self.assertEqual(self.index.findings(), [])


class IndexResolutionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        corpus = SynthCorpus(self.tmp / "corpus")
        corpus.add("rec-a")
        self.path = self.tmp / "index.sqlite"
        self.snapshot = build_index(corpus.root, self.path).snapshot_id

    def tearDown(self):
        self._tmp.cleanup()

    def assert_unavailable(self, source: str, problem_start: str, path=None):
        with self.assertRaises(LensError) as ctx:
            Index.open(path)
        error = ctx.exception
        self.assertEqual((error.code, error.exit_code), ("INDEX_UNAVAILABLE", 5))
        self.assertIn("index build --corpus", error.hint)
        self.assertIn(ENV_VAR, error.hint)
        [tried] = error.details["tried"]
        self.assertEqual(tried["source"], source)
        self.assertTrue(tried["problem"].startswith(problem_start), tried)

    def test_resolution_order_and_failures(self):
        with mock.patch.dict(os.environ, {ENV_VAR: str(self.path)}):
            with Index.open() as index:
                self.assertEqual((index.source, index.snapshot_id), ("env", self.snapshot))
            with Index.open(self.path) as index:
                self.assertEqual(index.source, "argument")
            self.assert_unavailable("argument", "missing", self.tmp / "nope.sqlite")
        with mock.patch.dict(os.environ, {ENV_VAR: str(self.tmp / "nope.sqlite")}):
            self.assert_unavailable("env", "missing")
        garbage = self.tmp / "garbage.sqlite"
        garbage.write_bytes(b"this is not sqlite" * 100)
        self.assert_unavailable("argument", "corrupt", garbage)
        other = self.tmp / "other.sqlite"
        with sqlite3.connect(other) as conn:
            conn.execute("CREATE TABLE t(x)")
        conn.close()
        self.assert_unavailable("argument", "not an os-optics-lens index", other)
        self.assert_unavailable("argument", "not a file", self.tmp)

    def test_schema_mismatch(self):
        copy_path = self.tmp / "old.sqlite"
        copy_path.write_bytes(self.path.read_bytes())
        conn = sqlite3.connect(copy_path)
        conn.execute("UPDATE meta SET value='0' WHERE key='index_schema_version'")
        conn.commit()
        conn.close()
        self.assert_unavailable("argument", "schema mismatch", copy_path)

    def test_bundled_index_absent_in_source_tree(self):
        bundled = ROOT / "src" / "os_optics_lens" / "_data" / "index.sqlite"
        if bundled.exists():
            self.skipTest("a bundled index exists in the source tree")
        env = {key: value for key, value in os.environ.items() if key != ENV_VAR}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assert_unavailable("bundled", "missing")

    def test_read_only_open_creates_no_sidecars(self):
        with Index.open(self.path) as index:
            index.meta()
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["corpus", "index.sqlite"])


if __name__ == "__main__":
    unittest.main()

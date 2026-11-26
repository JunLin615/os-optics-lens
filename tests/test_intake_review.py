"""Regressions for the PR #40 acceptance review: evidence integrity, intake type findings, the
explain-request budget, reader/index semantics binding, the module_role facet and the evidence
extract contract.

User-visible failures are reproduced through the public CLI (`python -m os_optics_lens` as a
subprocess); focused unit tests cover the checks underneath.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from os_optics_lens import output, query, semantics  # noqa: E402
from os_optics_lens.build import build_index  # noqa: E402
from os_optics_lens.canonical import record_hash  # noqa: E402
from os_optics_lens.corpus import evidence_owner_problem, load_corpus, non_object_sources  # noqa: E402
from os_optics_lens.errors import LensError  # noqa: E402
from os_optics_lens.evaluate import unit_for_name  # noqa: E402
from os_optics_lens.normalize import unit_for  # noqa: E402
from os_optics_lens.store import Index, open_index, resolve_source_in  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


helpers = _load("_intake_build_helpers", ROOT / "tests/test_build_foundation.py")
fuzz = _load("_intake_fuzz", ROOT / "tests/intake_fuzz.py")
combined = _load("_verify_combined_intake", ROOT / "scripts/verify_combined_intake.py")

P2B = "yutong-2025-mirrorless-18-p2b"
P2B_EVIDENCE = f"records/yutong-cup/{P2B}/evidence.json"
FIXTURE = ROOT / "acceptance/yutong-2025-mirrorless-problem-2.json"
MARKER = "TAMPERED-EVIDENCE-MARKER"

_TMP: tempfile.TemporaryDirectory | None = None
TMP = Path()
INDEX = Path()


def setUpModule() -> None:
    global _TMP, TMP, INDEX
    _TMP = tempfile.TemporaryDirectory(prefix="os-optics-lens-intake-")
    TMP = Path(_TMP.name)
    INDEX = TMP / "checkout.sqlite"
    build_index(ROOT, INDEX)


def tearDownModule() -> None:
    if _TMP is not None:
        _TMP.cleanup()


def run_cli(*args: str, index: Path | None = None, request: Any = None,
            package_root: Path = SRC) -> tuple[int, dict[str, Any], bytes]:
    """(exit code, response, raw stdout) of one CLI invocation; `request` goes to `search -`."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("OS_OPTICS_LENS_INDEX", "OS_OPTICS_LENS_DEBUG", "PYTHONIOENCODING", "PYTHONUTF8")}
    env["PYTHONPATH"] = str(package_root)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    argv = [sys.executable, "-m", "os_optics_lens", *args]
    if index is not None:
        argv += ["--index", str(index)]
    data = b""
    if request is not None:
        argv.append("-")
        data = json.dumps(request).encode("utf-8")
    proc = subprocess.run(argv, input=data, capture_output=True, env=env, cwd=str(TMP), timeout=300)
    assert proc.stderr == b"", proc.stderr
    return proc.returncode, json.loads(proc.stdout.decode("ascii")), proc.stdout


class CliCase(unittest.TestCase):
    def ok(self, *args: str, **kwargs: Any) -> dict[str, Any]:
        code, doc, _ = run_cli(*args, **kwargs)
        self.assertEqual((code, doc["ok"]), (0, True), doc)
        return doc

    def fails(self, error: str, exit_code: int, *args: str, **kwargs: Any) -> dict[str, Any]:
        code, doc, raw = run_cli(*args, **kwargs)
        self.assertFalse(doc["ok"], doc)
        self.assertEqual((doc["error"]["code"], code), (error, exit_code), doc)
        return doc["error"]


# F6 evidence bytes are verified before use ------------------------------------------------------


def tampered(name: str, sql: str, params: tuple[Any, ...]) -> Path:
    """A copy of the checkout index with one statement applied to it."""
    path = TMP / f"{name}.sqlite"
    shutil.copyfile(INDEX, path)
    with contextlib.closing(sqlite3.connect(path)) as conn, conn:
        self_check = conn.execute(sql, params)
        assert self_check.rowcount == 1, sql
    return path


def altered_extract() -> bytes:
    """The P2b extract with every section altered but still a conforming, plausible document."""
    document = json.loads((ROOT / P2B_EVIDENCE).read_bytes())
    document["sections"] = {name: {**value, "note": MARKER} if isinstance(value, dict) else MARKER
                            for name, value in document["sections"].items()}
    return json.dumps(document).encode("utf-8")


class EvidenceIntegrityCli(CliCase):
    """Altered evidence bytes with an intact digest column (and meta) are never used."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.damaged = tampered("content", "UPDATE evidence_files SET content=? WHERE path=?",
                               (altered_extract(), P2B_EVIDENCE))

    def constraints(self, index: Path) -> dict[str, Any]:
        doc = self.ok("search", "--fixture", str(FIXTURE), "--max-output-bytes", "262144", index=index)
        [candidate] = doc["result"]["candidates"]
        return candidate["constraints"]

    def test_evidence_requiring_constraints_are_unconfirmed(self) -> None:
        intact, damaged = self.constraints(INDEX), self.constraints(self.damaged)
        requiring = {c["id"] for c in json.loads(FIXTURE.read_bytes())["constraints"]
                     if c.get("requires_evidence_source")
                     or any(p["op"].startswith("has_source") for p in c["predicates"])}
        self.assertTrue(requiring)
        for cid in requiring:
            with self.subTest(constraint=cid):
                self.assertEqual(intact[cid]["outcome"], "MATCH")
                self.assertEqual((damaged[cid]["outcome"], damaged[cid]["reason"]),
                                 ("UNCONFIRMED", "EVIDENCE_UNRESOLVED"))
        for cid in set(intact) - requiring:
            self.assertEqual(damaged[cid]["outcome"], intact[cid]["outcome"], cid)

    def test_inspect_evidence_fails_without_echoing_altered_bytes(self) -> None:
        for name in ("back_focus", "all", f"{P2B_EVIDENCE}#back_focus", "no_such_section"):
            with self.subTest(evidence=name):
                code, doc, raw = run_cli("inspect", P2B, "--evidence", name, index=self.damaged)
                self.assertEqual((doc["error"]["code"], code), ("INDEX_UNAVAILABLE", 5), doc)
                self.assertIn("damaged", doc["error"]["message"])
                self.assertIn("index build", doc["error"]["hint"])
                self.assertIn("damaged", doc["error"]["details"]["tried"][0]["problem"])
                self.assertNotIn(MARKER.encode(), raw)

    def test_supporting_evidence_listing_shows_the_live_status(self) -> None:
        code, doc, raw = run_cli("inspect", P2B, "--fields", "performance.mtf_field_minimum", index=self.damaged)
        self.assertEqual(code, 0, doc)
        self.assertNotIn(MARKER.encode(), raw)
        result = doc["result"]
        statuses = {r["status"] for r in result["evidence"]["refs"] if r["path"] == P2B_EVIDENCE}
        self.assertEqual(statuses, {"HASH_MISMATCH"})
        self.assertEqual(result["evidence"]["files"][P2B_EVIDENCE]["status"], "HASH_MISMATCH")
        self.assertNotIn(P2B_EVIDENCE, result["evidence"]["sections"])
        fact = result["fields"]["performance.mtf_field_minimum"]
        self.assertNotIn("source", fact)
        self.assertNotIn("source", fact["value"])
        intact = self.ok("inspect", P2B, index=INDEX)["result"]["evidence"]
        self.assertEqual({r["status"] for r in intact["refs"]}, {"RESOLVED"})
        self.assertNotIn("status", intact["files"][P2B_EVIDENCE])

    def test_other_kinds_of_damage(self) -> None:
        payload = altered_extract()
        content = (ROOT / P2B_EVIDENCE).read_bytes().replace(b"\r\n", b"\n")
        cases = {
            # bytes and digest column rewritten consistently: the references still expect the original
            "consistent": ("UPDATE evidence_files SET content=?, sha256=? WHERE path=?",
                           (payload, hashlib.sha256(payload).hexdigest(), P2B_EVIDENCE), "HASH_MISMATCH"),
            "digest-column": ("UPDATE evidence_files SET sha256=? WHERE path=?", ("0" * 64, P2B_EVIDENCE),
                              "HASH_MISMATCH"),
            "stored-as-text": ("UPDATE evidence_files SET content=? WHERE path=?",
                               (content.decode("utf-8"), P2B_EVIDENCE), "HASH_MISMATCH"),
            "deleted": ("DELETE FROM evidence_files WHERE path=?", (P2B_EVIDENCE,), "FILE_MISSING"),
        }
        for name, (sql, params, status) in cases.items():
            with self.subTest(damage=name):
                index = tampered(name, sql, params)
                result = self.constraints(index)["mtf_0_to_0p8"]
                self.assertEqual((result["outcome"], result["reason"]), ("UNCONFIRMED", "EVIDENCE_UNRESOLVED"))
                listing = self.ok("inspect", P2B, index=index)["result"]["evidence"]
                self.assertEqual({r["status"] for r in listing["refs"] if r["path"] == P2B_EVIDENCE}, {status})
                self.fails("INDEX_UNAVAILABLE", 5, "inspect", P2B, "--evidence", "all", index=index)


class EvidenceIntegrityUnit(unittest.TestCase):
    CONTENT = b'{"schema": "opensci.lens.evidence_extract", "sections": {"mtf": 1}}'
    DIGEST = hashlib.sha256(CONTENT).hexdigest()
    SOURCE = {"path": "e.json", "sha256": DIGEST, "section": "mtf"}

    def test_bytes_must_hash_to_both_digests(self) -> None:
        cache: dict[tuple[str, str], Any] = {}
        altered = self.CONTENT.replace(b"1", b"2")
        for stored, content in ((self.DIGEST, altered), ("0" * 64, self.CONTENT),
                                (hashlib.sha256(altered).hexdigest(), altered), (self.DIGEST, "text"),
                                (self.DIGEST, None)):
            result = resolve_source_in({"e.json": (stored, content)}, self.SOURCE, cache)
            self.assertEqual(result["status"], "HASH_MISMATCH")
            self.assertNotIn("section_value", result)
        self.assertEqual(cache, {}, "unverified content must never be cached")

    def test_a_cached_parse_does_not_vouch_for_later_bytes(self) -> None:
        cache: dict[tuple[str, str], Any] = {}
        files = {"e.json": (self.DIGEST, self.CONTENT)}
        self.assertEqual(resolve_source_in(files, self.SOURCE, cache)["section_value"], 1)
        files["e.json"] = (self.DIGEST, self.CONTENT.replace(b"1", b"2"))
        self.assertEqual(resolve_source_in(files, self.SOURCE, cache)["status"], "HASH_MISMATCH")

    def test_references_unresolved_at_build_are_not_damage(self) -> None:
        with tempfile.TemporaryDirectory(dir=TMP) as scratch:
            root = Path(scratch) / "corpus"
            corpus = helpers.SynthCorpus(root)

            def wrong_digest(record: dict[str, Any]) -> None:
                record["performance"][0]["source"]["sha256"] = "0" * 64

            corpus.add("rec-a", mutate=wrong_digest)
            out = Path(scratch) / "index.sqlite"
            build_index(root, out, allow_unresolved_evidence=True)
            with open_index(out) as index:
                result = query.inspect(index, "rec-a", evidence=["all"])["result"]
                self.assertEqual(result["evidence_sections"], {})
                [ref] = [r for r in result["evidence"]["refs"] if r["section"] == "mtf"]
                self.assertEqual(ref["status"], "HASH_MISMATCH")


# F7 strict intake reports bad roots and types as findings ----------------------------------------


class IntakeCase(CliCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(dir=TMP)
        self.work = Path(self._tmp.name)
        self.root = self.work / "corpus"
        self.corpus = helpers.SynthCorpus(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def validate_errors(self) -> list[dict[str, Any]]:
        error = self.fails("BUILD_FAILED", 6, "index", "validate", "--corpus", str(self.root))
        return [f for f in error["details"]["findings"] if f["severity"] == "error"]

    def write(self, relative: str, text: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))


class DuplicateMemberCli(IntakeCase):
    """A repeated member name would otherwise be resolved silently (the last one wins), and the
    record hash, computed from the parsed record, cannot see it."""

    def test_repeated_member_names_are_findings_and_keep_the_previous_index(self) -> None:
        self.corpus.add("rec-a")
        out = self.work / "index.sqlite"
        self.ok("index", "build", "--corpus", str(self.root), "--out", str(out))
        before = out.read_bytes()
        record = "records/synth/rec-a/record.json"
        project = "records/synth/rec-a/project.json"
        cases = ((record, "MALFORMED_JSON"), (project, "PROJECT_MALFORMED"),
                 ("records/synth/batch-dup.json", "MALFORMED_JSON"))
        for relative, code in cases:
            with self.subTest(file=relative):
                path = self.root / relative
                original = path.read_bytes() if path.is_file() else None
                text = original.decode("utf-8") if original else "{}"
                body = text.lstrip()[1:].lstrip()
                separator = "" if body.startswith("}") else ","
                self.write(relative, '{"schema": "first", "schema": "second"' + separator + body)
                error = self.fails("BUILD_FAILED", 6, "index", "build", "--corpus", str(self.root),
                                   "--out", str(out))
                codes = {f["code"] for f in error["details"]["findings"]}
                self.assertIn(code, codes)
                self.assertEqual(out.read_bytes(), before)
                if original is None:
                    path.unlink()
                else:
                    path.write_bytes(original)
        self.ok("index", "validate", "--corpus", str(self.root))


class BadRootsCli(IntakeCase):
    def test_non_object_records_and_manifests_are_findings(self) -> None:
        self.corpus.add("rec-a")
        for text in ("null", "[]", '"x"', "1", "true"):
            for relative in ("records/incoming/novel/record.json", "records/incoming/batch-new.json"):
                with self.subTest(file=relative, root=text):
                    self.write(relative, text + "\n")
                    [error] = self.validate_errors()
                    self.assertEqual((error["code"], error["file"], error["field"]),
                                     ("MALFORMED_JSON", relative, ""))
                    (self.root / relative).unlink()
        self.ok("index", "validate", "--corpus", str(self.root))

    def test_null_project_and_evidence_are_findings(self) -> None:
        for record_id, name, code in (("rec-p", "project.json", "PROJECT_MALFORMED"),
                                      ("rec-e", "evidence.json", "EVIDENCE_MALFORMED")):
            def point(record: dict[str, Any], name: str = name) -> None:
                digest = hashlib.sha256(b"null\n").hexdigest()
                target = record["native_project"] if name == "project.json" else record["performance"][0]["source"]
                target["sha256"] = digest

            self.corpus.add(record_id, mutate=point)
            self.write(f"records/synth/{record_id}/{name}", "null\n")
        found = {(f["record_id"], f["code"]) for f in self.validate_errors()}
        self.assertEqual(found, {("rec-p", "PROJECT_MALFORMED"), ("rec-e", "EVIDENCE_MALFORMED")})


class MemberTypesCli(IntakeCase):
    def test_list_collection_id_is_a_finding_not_a_crash(self) -> None:
        self.corpus.add("rec-a", mutate=lambda r: r["collection"].__setitem__("collection_id", ["synth"]))
        self.write("records/synth/batch-synth.json", json.dumps(
            {"collection": {"collection_id": "synth"}, "accepted_record_ids": []}))
        errors = self.validate_errors()
        self.assertIn(("INVALID_FIELD", "records/synth/rec-a/record.json", "collection.collection_id"),
                      {(f["code"], f["file"], f["field"]) for f in errors})
        self.fails("BUILD_FAILED", 6, "index", "build", "--corpus", str(self.root), "--out",
                   str(self.work / "x.sqlite"))

    def test_manifest_members(self) -> None:
        self.corpus.add("rec-a")
        for manifest, code, field in (
                ({"accepted_record_ids": []}, "MISSING_FIELD", "collection"),
                ({"collection": "synth", "accepted_record_ids": []}, "INVALID_FIELD", "collection"),
                ({"collection": {"collection_id": ["synth"]}, "accepted_record_ids": []}, "INVALID_FIELD",
                 "collection.collection_id"),
                ({"collection": {"collection_id": "synth"}, "accepted_record_ids": [["rec-a"]]},
                 "INVALID_FIELD", "accepted_record_ids")):
            with self.subTest(field=field, code=code):
                self.write("records/synth/batch-synth.json", json.dumps(manifest))
                self.assertEqual({(f["code"], f["field"]) for f in self.validate_errors()}, {(code, field)})

    def test_mistyped_members_are_findings_naming_the_field(self) -> None:
        def mutate(record: dict[str, Any]) -> None:
            record["design"].update(module_role=3, aperture_validity=[], design_scope=["x"])
            record["collection"]["license"] = "MIT"
            record["collection"]["evidence_extract"] = "evidence.json"
            record["native_project"]["engine_version"] = {"a": 1}
            record["validation"]["paraxial"] = 1
            record["spectrum"]["continuous_coverage_nm"]["state"] = ["x"]
            record["image_format"] = "full frame"
            record["field"]["kind_basis"] = 1
            record["performance"][0].update(conditions=["x"], sampled_field_fractions="0,1", source="evidence.json")
            record["aperture"]["f_number"]["system_aperture"] = "yes"
            record["analysis_evidence"] = {"ghost": "none"}

        self.corpus.add("rec-a", mutate=mutate)
        found = {(f["code"], f["field"]) for f in self.validate_errors()}
        for expected in (
                ("INVALID_FIELD", "design.module_role"), ("INVALID_FIELD", "design.aperture_validity"),
                ("INVALID_FIELD", "design.design_scope"), ("INVALID_FIELD", "collection.license"),
                ("EVIDENCE_MALFORMED", "collection.evidence_extract"),
                ("INVALID_FIELD", "native_project.engine_version"), ("INVALID_FIELD", "validation.paraxial"),
                ("INVALID_FIELD", "spectrum.continuous_coverage_nm.state"), ("INVALID_FIELD", "image_format"),
                ("INVALID_FIELD", "field.kind_basis"), ("INVALID_FIELD", "performance[0].conditions"),
                ("INVALID_FIELD", "performance[0].sampled_field_fractions"),
                ("EVIDENCE_MALFORMED", "performance[0].source"),
                ("INVALID_FIELD", "aperture.f_number.system_aperture"), ("INVALID_FIELD", "analysis_evidence.ghost")):
            self.assertIn(expected, found)

    def test_mistyped_source_members_build_when_unresolved_evidence_is_allowed(self) -> None:
        # The fact-to-reference link keys on the source's strings; a list used to be unhashable.
        for position, member in enumerate(("path", "sha256", "section")):
            self.corpus.add(f"rec-{position}",
                            mutate=lambda r, m=member: r["performance"][0]["source"].__setitem__(m, ["x"]))
        self.validate_errors()
        out = self.work / "allowed.sqlite"
        self.ok("index", "build", "--corpus", str(self.root), "--out", str(out), "--allow-unresolved-evidence")
        for position in range(3):
            self.ok("inspect", f"rec-{position}", "--evidence", "all", index=out)

    def test_project_configuration_label_must_be_text(self) -> None:
        project = helpers.minimal_project([{"configuration_id": "Main", "label": 3}])
        self.corpus.add("rec-a", project=project)
        self.assertEqual({(f["code"], f["field"]) for f in self.validate_errors()},
                         {("PROJECT_CONFIGURATION_INVALID", "configurations[0].label")})

    def test_failed_rebuild_keeps_the_previous_index_byte_identical(self) -> None:
        self.corpus.add("rec-a")
        out = self.work / "out" / "index.sqlite"
        self.ok("index", "build", "--corpus", str(self.root), "--out", str(out))
        before = out.read_bytes()
        self.corpus.add("rec-b", mutate=lambda r: r["collection"].__setitem__("collection_id", ["synth"]))
        self.corpus.add("rec-c", mutate=lambda r: r["performance"][0].__setitem__("conditions", 1))
        self.write("records/incoming/novel/record.json", "null\n")
        self.write("records/incoming/batch-new.json", "null\n")
        self.write("records/synth/rec-a/evidence.json", "[]\n")
        self.fails("BUILD_FAILED", 6, "index", "build", "--corpus", str(self.root), "--out", str(out))
        self.assertEqual(out.read_bytes(), before)
        self.assertEqual([p.name for p in out.parent.iterdir()], ["index.sqlite"])
        self.ok("info", index=out)


class TypeFuzzSubset(unittest.TestCase):
    """A representative subset of the full type fuzz (tests/intake_fuzz.py): the paths that crashed
    the builder or a query before the type checks, over a two-configuration record."""

    CASE = fuzz.two_configuration_case()
    RECORD = CASE.record
    PROJECT = f"{fuzz.SYNTH_DIR}/project.json"
    EVIDENCE = f"{fuzz.SYNTH_DIR}/evidence.json"
    MANIFEST = "records/synth/batch-synth-1.json"
    ENTRY = ("configurations", 0)
    MUTATIONS = (
        (RECORD, ("collection", "collection_id"), ["x"]),
        (RECORD, ("collection", "evidence_extract"), "x"),
        (RECORD, ("design", "module_role"), {"a": 1}),
        (RECORD, ("design", "module_role"), "x"),
        (RECORD, ("spectrum", "continuous_coverage_nm", "state"), ["x"]),
        (RECORD, ENTRY + ("field", "field_points_deg"), 1),
        (RECORD, ENTRY + ("field", "half_field_deg"), "x"),
        (RECORD, ENTRY + ("performance", 0, "conditions"), ["x"]),
        (RECORD, ENTRY + ("performance", 0, "sampled_field_fractions"), 1.5),
        (RECORD, ENTRY + ("performance", 0, "source", "path"), ["x"]),
        (RECORD, ENTRY + ("performance", 0, "source", "section"), {"a": 1}),
        (RECORD, ("analysis_evidence", "ghost", "methods"), {"a": 1}),
        (RECORD, ("cost_proxy", "spherical_element_weight"), ["x"]),
        (PROJECT, ("optical_models",), 1),
        (PROJECT, ("optical_models", 0, "model"), "x"),
        (PROJECT, ("optical_models", 0, "model", "members", 0, "pose"), "x"),
        (PROJECT, ("components", 0, "optical_role"), ["x"]),
        (PROJECT, ("model_materials",), True),
        (PROJECT, ("observations",), {"a": 1}),
        (PROJECT, ("configurations", 1, "name"), ["x"]),
        (EVIDENCE, (), None),
        (EVIDENCE, ("sections",), ["x"]),
        (EVIDENCE, ("sections", "mtf"), None),
        (MANIFEST, (), None),
        (MANIFEST, ("collection",), []),
        (MANIFEST, ("accepted_record_ids", 0), {}),
    )

    def test_representative_mutations(self) -> None:
        with tempfile.TemporaryDirectory(dir=TMP) as scratch:
            work = Path(scratch)
            self.CASE.write(work / "corpus")
            build_index(work / "corpus", work / "base.sqlite")
            self.assertEqual(fuzz.exercise(work / "base.sqlite"), [])
            statuses = set()
            for mutated, path, value in self.MUTATIONS:
                with self.subTest(file=mutated.rsplit("/", 1)[1], path=path, value=value):
                    status, problems = fuzz.run_mutation(self.CASE, work, mutated, path, value)
                    statuses.add(status)
                    self.assertEqual(problems, [])
            self.assertEqual(statuses, {"strict", "allowed", "rejected"})


# F8 explain-request honors the request's max_output_bytes ----------------------------------------


def big_request(**extra: Any) -> dict[str, Any]:
    constraints = [{"id": f"c{i}", "fact": "first_order.effective_focal_length_mm", "op": "lte", "value": 100 + i}
                   for i in range(60)]
    return {"mode": "specification", "constraints": constraints, **extra}


class ExplainBudgetCli(CliCase):
    def check_limit(self, request: dict[str, Any], budget: int) -> None:
        code, doc, raw = run_cli("search", "--explain-request", index=INDEX, request=request)
        self.assertEqual((doc["error"]["code"], code), ("OUTPUT_LIMIT_EXCEEDED", 2), doc)
        self.assertEqual(doc["error"]["details"]["max_output_bytes"], budget)
        self.assertGreater(doc["error"]["details"]["required_bytes"], budget)
        self.assertLessEqual(len(raw), budget)

    def test_single_and_batch_use_the_request_budget(self) -> None:
        batch = {"queries": [big_request(), big_request()], "max_output_bytes": 2048}
        self.check_limit(big_request(max_output_bytes=2048), 2048)
        self.check_limit(batch, 2048)

    def test_a_larger_request_budget_is_used(self) -> None:
        batch = {"queries": [big_request() for _ in range(12)]}
        self.check_limit(batch, 32768)
        doc = self.ok("search", "--explain-request", index=INDEX, request={**batch, "max_output_bytes": 262144})
        self.assertEqual(doc["bounds"]["max_output_bytes"], 262144)
        self.assertGreater(doc["bounds"]["output_bytes"], 32768)

    def test_the_argument_wins(self) -> None:
        doc = self.ok("search", "--explain-request", "--max-output-bytes", "262144", index=INDEX,
                      request=big_request(max_output_bytes=2048))
        self.assertEqual(doc["bounds"]["max_output_bytes"], 262144)

    def test_other_explain_errors_are_bounded_by_the_request_budget(self) -> None:
        bad = {"mode": "specification", "constraints": [{"fact": "x." + "y" * 6000, "op": "zz", "value": 1}],
               "max_output_bytes": 2048}
        code, doc, raw = run_cli("search", "--explain-request", index=INDEX, request=bad)
        self.assertEqual((doc["error"]["code"], code), ("UNSUPPORTED_OPERATOR", 2))
        self.assertLessEqual(len(raw), 2048)


class ExplainBudgetUnit(unittest.TestCase):
    def test_errors_carry_the_request_budget(self) -> None:
        with open_index(INDEX) as index:
            for request in (big_request(max_output_bytes=2048), {"queries": [big_request()], "max_output_bytes": 2048}):
                for operation in (query.explain_request, query.run_request):
                    with self.subTest(operation=operation.__name__, batch="queries" in request):
                        try:
                            operation(index, request)
                        except LensError as error:
                            self.assertEqual(error.output_budget, 2048)


# F9 cursors and indexes are bound to the reader's semantics --------------------------------------


def reader_copy(name: str, *, newline: bytes = b"\n", edit: tuple[str, str, str] | None = None) -> Path:
    """A copy of the package (as a PYTHONPATH root), optionally with CRLF files or one edit."""
    root = TMP / "readers" / name
    shutil.copytree(SRC / "os_optics_lens", root / "os_optics_lens",
                    ignore=shutil.ignore_patterns("__pycache__", "_data"))
    for path in (root / "os_optics_lens").rglob("*"):
        if path.is_file():
            path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", newline))
    if edit is not None:
        file, old, new = edit
        path = root / "os_optics_lens" / file
        text = path.read_text(encoding="utf-8")
        assert text.count(old) == 1, (file, old)
        path.write_text(text.replace(old, new), encoding="utf-8")
    return root


STOP_GATE = ("vocabulary.json", '"facts": ["performance.mtf", "performance.mtf_field_minimum", ',
             '"facts": ["performance.mtf", ')
PAGED = {"mode": "starting_point", "limit": 2,
         "constraints": [{"fact": "performance.mtf_field_minimum", "op": "gte", "value": 0.5}]}


class SemanticsBindingCli(CliCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.changed_vocabulary = reader_copy("vocabulary", edit=STOP_GATE)

    def cursor(self) -> str:
        doc = self.ok("search", index=INDEX, request=PAGED)
        return doc["result"]["page"]["next_cursor"]

    def test_a_vocabulary_change_rejects_the_old_index_and_the_old_cursor(self) -> None:
        cursor = self.cursor()
        error = self.fails("INDEX_UNAVAILABLE", 5, "info", index=INDEX, package_root=self.changed_vocabulary)
        self.assertIn("vocabulary_digest", error["details"]["tried"][0]["problem"])
        self.assertIn("index build", error["hint"])
        rebuilt = TMP / "rebuilt.sqlite"
        self.ok("index", "build", "--corpus", str(ROOT), "--out", str(rebuilt),
                package_root=self.changed_vocabulary)
        self.ok("search", index=rebuilt, request=PAGED, package_root=self.changed_vocabulary)
        for extra in ((), ("--explain-request",)):
            error = self.fails("CURSOR_SEMANTICS_MISMATCH", 4, "search", *extra, index=rebuilt,
                               request={**PAGED, "cursor": cursor}, package_root=self.changed_vocabulary)
            self.assertIn("without a cursor", error["hint"])

    def test_query_code_changes_reject_cursors_but_not_the_index(self) -> None:
        cursor = self.cursor()
        reader = reader_copy("evaluate", edit=("evaluate.py", '_UNPINNED_LIMIT = 16\n', '_UNPINNED_LIMIT = 17\n'))
        self.ok("info", index=INDEX, package_root=reader)
        self.fails("CURSOR_SEMANTICS_MISMATCH", 4, "search", index=INDEX, request={**PAGED, "cursor": cursor},
                   package_root=reader)

    def test_indexing_code_changes_reject_the_index(self) -> None:
        reader = reader_copy("corpus", edit=("corpus.py", "MAX_REVISION = 2**63 - 1", "MAX_REVISION = 2**31 - 1"))
        error = self.fails("INDEX_UNAVAILABLE", 5, "info", index=INDEX, package_root=reader)
        self.assertIn("index_semantics", error["details"]["tried"][0]["problem"])

    def test_a_crlf_install_reads_the_same_index_and_cursor(self) -> None:
        reader = reader_copy("crlf", newline=b"\r\n")
        cursor = self.cursor()
        self.ok("search", index=INDEX, request={**PAGED, "cursor": cursor}, package_root=reader)

    def test_a_version_1_cursor_is_invalid_with_a_restart_hint(self) -> None:
        payload = json.dumps({"v": 1, "s": "0" * 16, "q": "0" * 16, "o": 2}, separators=(",", ":"))
        old = base64.urlsafe_b64encode(payload.encode()).rstrip(b"=").decode()
        error = self.fails("CURSOR_INVALID", 2, "search", index=INDEX, request={**PAGED, "cursor": old})
        self.assertIn("without a cursor", error["hint"])


class SemanticsBindingUnit(unittest.TestCase):
    def test_index_records_the_producer_fingerprint(self) -> None:
        with open_index(INDEX) as index:
            self.assertEqual(index.meta()["index_semantics"], semantics.index_fingerprint())

    def test_fingerprint_sources(self) -> None:
        self.assertEqual(set(semantics.READER_SOURCES), {
            "evaluate.py", "query.py", "output.py", "canonical.py", "normalize.py", "project_facts.py", "store.py",
            "vocabulary.py", "vocabulary.json"})
        self.assertEqual(set(semantics.INDEX_SOURCES), {
            "corpus.py", "normalize.py", "project_facts.py", "build.py", "canonical.py", "vocabulary.py",
            "vocabulary.json"})

    def test_an_index_without_the_fingerprint_is_refused(self) -> None:
        path = tampered("no-semantics", "DELETE FROM meta WHERE key=?", ("index_semantics",))
        with self.assertRaises(LensError) as ctx:
            Index.open(path)
        self.assertEqual(ctx.exception.code, "INDEX_UNAVAILABLE")

    def test_a_changed_loaded_vocabulary_refuses_the_index(self) -> None:
        with mock.patch("os_optics_lens.store.load_vocabulary",
                        return_value=mock.Mock(digest="f" * 64)), self.assertRaises(LensError) as ctx:
            Index.open(INDEX)
        self.assertIn("vocabulary_digest", ctx.exception.details["tried"][0]["problem"])

    def test_cursor_carries_the_reader(self) -> None:
        cursor = output.encode_cursor("s" * 16, "q" * 16, 3)
        self.assertEqual(output.decode_cursor(cursor, "s" * 16, "q" * 16), 3)
        with mock.patch("os_optics_lens.output.reader_fingerprint", return_value="0" * 64):
            with self.assertRaises(LensError) as ctx:
                output.decode_cursor(cursor, "s" * 16, "q" * 16)
        self.assertEqual((ctx.exception.code, ctx.exception.exit_code), ("CURSOR_SEMANTICS_MISMATCH", 4))


# F10 module_role and the evidence extract contract -----------------------------------------------


class ModuleRoleCli(IntakeCase):
    def test_facet_terms_aliases_and_verbatim_unknowns(self) -> None:
        for record_id, role in (("rec-a", "main_camera"), ("rec-b", "Ultra-Wide"), ("rec-c", "tele"),
                                ("rec-d", "Macro Module"), ("rec-e", "front")):
            self.corpus.add(record_id, mutate=lambda r, role=role: r["design"].__setitem__("module_role", role))
        out = self.work / "roles.sqlite"
        built = self.ok("index", "build", "--corpus", str(self.root), "--out", str(out))
        self.assertEqual([(f["code"], f["record_id"], f["field"]) for f in built["result"]["findings"]],
                         [("UNKNOWN_TERM", "rec-d", "design.module_role")])
        facet = self.ok("facets", "--facet", "module_role", index=out)["result"]["facets"]["module_role"]
        self.assertEqual(facet["level"], "record")
        self.assertEqual(dict(facet["values"]), {"main_camera": 1, "ultrawide": 1, "telephoto": 1,
                                                 "front_camera": 1, "Macro Module": 1})
        for requested, expected in (("main", "rec-a"), ("ultra_wide", "rec-b"), ("telephoto", "rec-c"),
                                    ("Macro Module", "rec-d"), ("front_camera", "rec-e")):
            with self.subTest(requested=requested):
                doc = self.ok("search", "--mode", "starting_point", "--filter", f"module_role={requested}",
                              index=out)
                self.assertEqual([c["record_id"] for c in doc["result"]["candidates"]], [expected])

    def test_non_string_module_role_is_invalid(self) -> None:
        for position, value in enumerate((None, 3, ["telephoto"], "  ")):
            self.corpus.add(f"rec-{position}",
                            mutate=lambda r, v=value: r["design"].__setitem__("module_role", v))
        self.assertEqual({(f["code"], f["field"], f["record_id"]) for f in self.validate_errors()},
                         {("INVALID_FIELD", "design.module_role", f"rec-{i}") for i in range(4)})

    def test_a_known_role_with_no_records_is_an_empty_result(self) -> None:
        doc = self.ok("search", "--mode", "starting_point", "--filter", "module_role=front", index=INDEX)
        self.assertEqual(doc["result"]["candidates"], [])
        self.assertEqual([w["code"] for w in doc["warnings"]], ["FACET_VALUE_EMPTY"])


class EvidenceContractCli(IntakeCase):
    GOOD = {"schema": "opensci.lens.evidence_extract", "record_id": "rec-a", "sections": {"mtf": {"rows": []}}}
    BROKEN = (
        ([GOOD], "root"),
        ({k: v for k, v in GOOD.items() if k != "schema"}, "field schema"),
        ({**GOOD, "schema": "opensci.lens.evidence"}, "field schema"),
        ({"schema": GOOD["schema"], "record_id": "rec-a", "mtf": {"rows": []}}, "field sections"),
        ({**GOOD, "sections": {}}, "field sections"),
        ({**GOOD, "sections": [1]}, "field sections"),
        ({**GOOD, "sections": {"mtf": 1, "stated": None}}, "field sections.stated"),
    )

    def add(self, document: Any, reference: str) -> None:
        payload = json.dumps(document).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        path = "records/synth/rec-a/extract.json"

        def mutate(record: dict[str, Any]) -> None:
            ref = {"path": path, "sha256": digest}
            if reference == "extract":
                record["collection"]["evidence_extract"] = ref
            else:
                record["performance"] = [{"metric": "m", "value": 1, "source": {**ref, "section": "mtf"}}]

        shutil.rmtree(self.root / "records", ignore_errors=True)
        self.corpus.add("rec-a", with_evidence=False, mutate=mutate)
        self.write(path, payload.decode("utf-8"))

    def test_strict_intake_enforces_the_contract(self) -> None:
        for reference in ("extract", "source"):
            for document, named in self.BROKEN:
                with self.subTest(reference=reference, named=named, document=document):
                    self.add(document, reference)
                    [error] = self.validate_errors()
                    self.assertEqual(error["code"], "EVIDENCE_MALFORMED")
                    self.assertIn("records/synth/rec-a/extract.json", error["message"])
                    self.assertIn(named, error["message"])
        self.add(self.BROKEN[3][0], "extract")
        doc = self.ok("index", "validate", "--corpus", str(self.root), "--allow-unresolved-evidence")
        self.assertEqual([(f["severity"], f["code"]) for f in doc["result"]["findings"]],
                         [("warning", "EVIDENCE_MALFORMED")])

    def test_top_level_metadata_is_not_addressable(self) -> None:
        self.add({**self.GOOD, "source": {"publication": "x"}, "schema_version": "1.0"}, "extract")
        out = self.work / "index.sqlite"
        self.ok("index", "build", "--corpus", str(self.root), "--out", str(out))
        result = self.ok("inspect", "rec-a", "--evidence", "all", index=out)["result"]
        self.assertEqual([sorted(e["sections"]) for e in result["evidence_sections"].values()], [["mtf"]])
        self.fails("EVIDENCE_SECTION_NOT_FOUND", 3, "inspect", "rec-a", "--evidence", "source", index=out)

    def test_committed_extracts_comply(self) -> None:
        findings = load_corpus(ROOT).findings
        self.assertEqual([f.to_dict() for f in findings if f.severity == "error"], [])


class EvidenceOwnerCli(IntakeCase):
    """`collection.evidence_extract` must pin an extract whose `record_id` names this record."""

    def add_record(self, record_id: str, extract_of: str, extra: dict[str, Any] | None = None) -> None:
        """A record whose collection.evidence_extract is the extract file written by `extract_of`."""
        payload = (self.root / self.extract_path(extract_of)).read_bytes()
        reference = {"path": self.extract_path(extract_of), "sha256": hashlib.sha256(payload).hexdigest(),
                     **(extra or {})}
        self.corpus.add(record_id, with_evidence=False,
                        mutate=lambda record: record["collection"].__setitem__("evidence_extract", reference))

    @staticmethod
    def extract_path(record_id: str) -> str:
        return f"records/synth/{record_id}/extract.json"

    def write_extract(self, directory: str, **members: Any) -> None:
        self.write(self.extract_path(directory), json.dumps(
            {"schema": "opensci.lens.evidence_extract", "sections": {"prescription": {"rows": [1]}}, **members}))

    def test_missing_mistyped_or_foreign_record_id_is_malformed(self) -> None:
        for members, found in (({}, "it is missing"), ({"record_id": ["x"]}, "it is a list"),
                               ({"record_id": None}, "it is null"),
                               ({"record_id": "some-other-record"}, "it is 'some-other-record'")):
            for extra in (None, {"section": "prescription"}):
                with self.subTest(members=members, extra=extra):
                    shutil.rmtree(self.root / "records", ignore_errors=True)
                    self.write_extract("rec-a", **members)
                    self.add_record("rec-a", "rec-a", extra)
                    [error] = self.validate_errors()
                    self.assertEqual((error["code"], error["field"], error["record_id"]),
                                     ("EVIDENCE_MALFORMED", "collection.evidence_extract.path", "rec-a"))
                    for text in (self.extract_path("rec-a"), "field record_id", found):
                        self.assertIn(text, error["message"])
                    self.fails("BUILD_FAILED", 6, "index", "build", "--corpus", str(self.root), "--out",
                               str(self.work / "x.sqlite"))

    def test_another_records_extract_is_never_shown(self) -> None:
        self.write_extract("rec-a", record_id="rec-a")
        self.add_record("rec-a", "rec-a")
        for extra in (None, {"section": "prescription"}):
            with self.subTest(extra=extra):
                shutil.rmtree(self.root / "records/synth/rec-b", ignore_errors=True)
                self.add_record("rec-b", "rec-a", extra)
                self.assertEqual({(f["code"], f["record_id"]) for f in self.validate_errors()},
                                 {("EVIDENCE_MALFORMED", "rec-b")})
                out = self.work / "allowed.sqlite"
                doc = self.ok("index", "build", "--corpus", str(self.root), "--out", str(out),
                              "--allow-unresolved-evidence")
                self.assertIn(("warning", "EVIDENCE_MALFORMED", "rec-b"),
                              {(f["severity"], f["code"], f["record_id"]) for f in doc["result"]["findings"]})
                # rec-a stored the bytes; rec-b's rejected reference still reaches none of them.
                own = self.ok("inspect", "rec-a", "--evidence", "prescription", index=out)["result"]
                self.assertEqual(list(own["evidence_sections"]), [self.extract_path("rec-a")])
                listing = self.ok("inspect", "rec-b", index=out)["result"]["evidence"]
                self.assertEqual((listing["sections"], listing["files"]), ({}, {}))
                self.assertEqual({r["status"] for r in listing["refs"]}, {"MALFORMED"})
                self.fails("EVIDENCE_SECTION_NOT_FOUND", 3, "inspect", "rec-b", "--evidence", "prescription",
                           index=out)
                shown = self.ok("inspect", "rec-b", "--evidence", "all", index=out)["result"]
                self.assertEqual(shown["evidence_sections"], {})

    def test_section_sources_may_name_another_records_file(self) -> None:
        self.write_extract("rec-a", record_id="rec-a")
        self.add_record("rec-a", "rec-a")
        digest = hashlib.sha256((self.root / self.extract_path("rec-a")).read_bytes()).hexdigest()
        source = {"path": self.extract_path("rec-a"), "sha256": digest, "section": "prescription"}
        self.corpus.add("rec-b", with_evidence=False, mutate=lambda record: record.__setitem__(
            "performance", [{"metric": "m", "value": 1, "source": source}]))
        doc = self.ok("index", "validate", "--corpus", str(self.root))
        self.assertEqual([f for f in doc["result"]["findings"] if f["code"].startswith("EVIDENCE_")], [])

    def test_owner_check(self) -> None:
        self.assertIsNone(evidence_owner_problem({"record_id": "rec-a"}, "rec-a"))
        for document in ({}, {"record_id": "rec-b"}, {"record_id": ["rec-a"]}, {"record_id": 1},
                         {"record_id": None}, {"record_id": {"a": 1}}):
            self.assertIn("field record_id", evidence_owner_problem(document, "rec-a"), document)


class SourceMemberCli(IntakeCase):
    """A `source` anywhere in the fact data is an evidence reference object or null."""

    BAD = ("records/synth/rec-a/evidence.json#mtf", ["not", "a", "ref"], [{"path": "does/not/exist.json"}], 3)

    def yutong_copy(self, mutate) -> None:
        """The committed camera collection in the scratch corpus, with P2b mutated and rehashed."""
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.copytree(ROOT / "records/yutong-cup", self.root / "records/yutong-cup")
        shutil.copytree(ROOT / "licenses", self.root / "licenses")
        shutil.copyfile(ROOT / "LICENSE", self.root / "LICENSE")
        path = self.root / "records/yutong-cup" / P2B / "record.json"
        record = json.loads(path.read_bytes())
        mutate(record)
        record["record_hash"] = record_hash(record)
        path.write_text(json.dumps(record), encoding="utf-8")

    def test_nested_non_object_sources_fail_strict_intake(self) -> None:
        def methods(record: dict[str, Any]) -> None:
            for method in record["analysis_evidence"]["ghost"]["methods"]:
                method["source"] = self.BAD[0]

        def gate(record: dict[str, Any]) -> None:
            record["analysis_evidence"]["tolerance"]["mtf_gate"]["source"] = self.BAD[1]

        def listed(record: dict[str, Any]) -> None:
            record["analysis_evidence"]["ghost"]["methods"][0]["source"] = self.BAD[2]

        count = len(json.loads((ROOT / "records/yutong-cup" / P2B / "record.json").read_bytes())
                    ["analysis_evidence"]["ghost"]["methods"])
        for mutate, fields in (
                (methods, {f"analysis_evidence.ghost.methods[{i}].source" for i in range(count)}),
                (gate, {"analysis_evidence.tolerance.mtf_gate.source"}),
                (listed, {"analysis_evidence.ghost.methods[0].source"})):
            with self.subTest(case=mutate.__name__):
                self.yutong_copy(mutate)
                errors = self.validate_errors()
                self.assertEqual({(f["code"], f["file"], f["field"]) for f in errors},
                                 {("EVIDENCE_MALFORMED", f"records/yutong-cup/{P2B}/record.json", name)
                                  for name in fields})
                self.fails("BUILD_FAILED", 6, "index", "build", "--corpus", str(self.root), "--out",
                           str(self.work / "x.sqlite"))
                doc = self.ok("index", "validate", "--corpus", str(self.root), "--allow-unresolved-evidence")
                self.assertEqual({(f["severity"], f["code"], f["field"]) for f in doc["result"]["findings"]
                                  if f["code"].startswith("EVIDENCE_")},
                                 {("warning", "EVIDENCE_MALFORMED", name) for name in fields})

    def test_every_depth_of_the_fact_data_is_checked_and_prose_sources_are_not(self) -> None:
        def mutate(record: dict[str, Any]) -> None:
            record["collection"]["source"] = "a publication"
            record["materials"] = {"model_glasses": [{"source": "a table"}]}
            record["design"]["history"] = {"steps": [[{"source": self.BAD[3]}]]}
            record["first_order"]["effective_focal_length_mm"]["detail"] = {"source": self.BAD[0]}
            record["performance"][0]["samples"] = [{"source": None}, {"source": self.BAD[1]}]
            record["extra_section"] = {"member": {"source": self.BAD[0]}}

        self.corpus.add("rec-a", mutate=mutate)
        self.assertEqual({(f["code"], f["field"]) for f in self.validate_errors()}, {
            ("EVIDENCE_MALFORMED", name) for name in (
                "design.history.steps[0][0].source", "first_order.effective_focal_length_mm.detail.source",
                "performance[0].samples[1].source", "extra_section.member.source")})

    def test_walk(self) -> None:
        data = {"a": {"source": "x"}, "b": [{"source": {"path": 1}}, {"source": None}, [{"source": []}]]}
        self.assertEqual(list(non_object_sources(data, "")), ["a.source", "b[2][0].source"])
        self.assertEqual(list(non_object_sources([{"source": True}], "configurations")),
                         ["configurations[0].source"])


class FractionOfUnitCli(IntakeCase):
    def test_a_fraction_of_something_is_dimensionless(self) -> None:
        name = "field_fraction_of_stated_half_field"
        self.corpus.add("rec-a", mutate=lambda record: record.__setitem__(
            "image_format", {name: {"value": 0.8, "basis": "ENGINE_COMPUTED"}}))
        doc = self.ok("index", "validate", "--corpus", str(self.root))
        self.assertNotIn("UNIT_UNKNOWN", {f["code"] for f in doc["result"]["findings"]})
        out = self.work / "index.sqlite"
        self.ok("index", "build", "--corpus", str(self.root), "--out", str(out))
        field = self.ok("fields", "--fact", f"image_format.{name}", index=out)["result"]
        self.assertIn('"unit": "1"', json.dumps(field))

    def test_build_and_evaluator_helpers_agree(self) -> None:
        for name in ("field_fraction_of_stated_half_field", "x_fraction_of_height_mm",
                     "y_fraction_of_field_from_dump", "z_fraction_of_nm"):
            self.assertEqual((unit_for(name), unit_for_name(name)), ("1", "1"), name)
        self.assertEqual((unit_for("height_mm"), unit_for_name("height_mm")), ("mm", "mm"))
        self.assertEqual((unit_for("fraction_kind"), unit_for_name("fraction_kind")), (None, None))


class WheelEvidenceCheckTests(unittest.TestCase):
    """The evidence assertion of scripts/verify_wheel.py (the installed-wheel run needs a build)."""

    def test_returned_content_and_statuses_are_asserted(self) -> None:
        wheel = _load("_verify_wheel_evidence", ROOT / "scripts/verify_wheel.py")
        extract = json.loads((ROOT / P2B_EVIDENCE).read_bytes())
        with open_index(INDEX) as index:
            doc = query.inspect(index, P2B, evidence=[wheel.EVIDENCE_SECTION], max_output_bytes=262144)
        self.assertEqual(wheel.EVIDENCE_PATH, P2B_EVIDENCE)
        self.assertEqual(wheel.evidence_failures(extract, doc), [])
        altered = {**extract, "sections": {**extract["sections"], wheel.EVIDENCE_SECTION: {"x": 1}}}
        self.assertTrue(wheel.evidence_failures(altered, doc))
        next(r for r in doc["result"]["evidence"]["refs"] if r["path"] == P2B_EVIDENCE)["status"] = "HASH_MISMATCH"
        self.assertTrue(wheel.evidence_failures(extract, doc))
        self.assertTrue(wheel.evidence_failures(extract, {"ok": False}))


class CombinedIntakeScriptTests(unittest.TestCase):
    """The path guard of scripts/verify_combined_intake.py (the end-to-end run needs the phone
    refs, which are not part of this tree)."""

    def test_paths_leaving_the_temporary_directory_are_refused(self) -> None:
        with tempfile.TemporaryDirectory(dir=TMP) as scratch:
            root = Path(scratch)
            prefix = "records/phone-modules/"
            good = combined.safe_target(root, prefix + "a/record.json", prefix)
            self.assertEqual(good, root / "records" / "phone-modules" / "a" / "record.json")
            for bad in (prefix + "../../../x", prefix + "a/../../x", "records/other/x", "/etc/passwd",
                        prefix + "a\\..\\x", prefix + "C:/x", prefix + "a//x", prefix + "./x",
                        prefix + "a/file:stream", prefix + "a\x00b", "../" + prefix + "x"):
                with self.subTest(path=bad), self.assertRaises(combined.UsageProblem):
                    combined.safe_target(root, bad, prefix)

    def test_symlinks_and_submodules_are_refused_before_anything_is_written(self) -> None:
        for mode, kind in (("120000", "blob"), ("160000", "commit")):
            listing = (f"100644 blob {'1' * 40}\trecords/phone-modules/a/record.json\0"
                       f"{mode} {kind} {'2' * 40}\trecords/phone-modules/a/link\0").encode()
            with tempfile.TemporaryDirectory(dir=TMP) as scratch, \
                    mock.patch.object(combined, "git", return_value=listing), \
                    self.assertRaises(combined.UsageProblem):
                try:
                    combined.extract_collection(Path(scratch), "0" * 40, "phone-modules", Path(scratch))
                finally:
                    self.assertEqual(list(Path(scratch).iterdir()), [])

    def test_an_unknown_ref_is_a_usage_error(self) -> None:
        proc = subprocess.run([sys.executable, str(ROOT / "scripts/verify_combined_intake.py"), "--repo",
                               str(TMP), "--phone-modules", "no-such-ref", "--phone-telephoto", "--bad"],
                              capture_output=True, timeout=120)
        self.assertEqual(proc.returncode, 2, proc.stderr)


if __name__ == "__main__":
    unittest.main()

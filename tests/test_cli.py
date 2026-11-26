"""Command-line interface: envelopes, exit codes, argparse errors as JSON, encodings and hand-off.

The CLI runs as a subprocess (`python -m os_optics_lens` with `PYTHONPATH=src`) against an index
built once from the checkout corpus into a temporary directory. Expected values (snapshot id,
Project hashes, fixture counts) are derived from the committed files, not hard-coded.
"""

from __future__ import annotations

import base64
import contextlib
import errno
import hashlib
import io
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

from os_optics_lens import cli, output  # noqa: E402
from os_optics_lens.build import build_index  # noqa: E402
from os_optics_lens.canonical import lf, record_hash  # noqa: E402
from os_optics_lens.errors import EXIT_CODES, LensError  # noqa: E402
from os_optics_lens.vocabulary import load_vocabulary  # noqa: E402

FIXTURE_PATH = ROOT / "acceptance" / "yutong-2025-mirrorless-problem-2.json"
CASE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
P2B = CASE["record"]["id"]
P2B_DIR = ROOT / "records" / "yutong-cup" / P2B

_TMP: tempfile.TemporaryDirectory | None = None
TMP: Path = Path()
INDEX: Path = Path()
SNAPSHOT = ""


def setUpModule() -> None:
    global _TMP, TMP, INDEX, SNAPSHOT
    _TMP = tempfile.TemporaryDirectory(prefix="os-optics-lens-cli-")
    TMP = Path(_TMP.name)
    INDEX = TMP / "index.sqlite"
    SNAPSHOT = build_index(ROOT, INDEX).snapshot_id


def tearDownModule() -> None:
    if _TMP is not None:
        _TMP.cleanup()


def base_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k not in ("OS_OPTICS_LENS_INDEX", "OS_OPTICS_LENS_DEBUG", "PYTHONIOENCODING", "PYTHONUTF8")}
    env["PYTHONPATH"] = str(SRC)
    env.update(extra)
    return env


class Run:
    """One finished CLI invocation."""

    def __init__(self, proc: subprocess.CompletedProcess) -> None:
        self.code: int = proc.returncode
        self.stdout: bytes = proc.stdout
        self.stderr: bytes = proc.stderr

    @property
    def doc(self) -> dict[str, Any]:
        return json.loads(self.stdout.decode("ascii"))


def run_cli(*args: str, index: bool = True, stdin: bytes | None = None, cwd: Path | None = None,
            **env: str) -> Run:
    argv = [sys.executable, "-m", "os_optics_lens"]
    if index:
        argv += ["--index", str(INDEX)]
    proc = subprocess.run(argv + list(args), input=stdin if stdin is not None else b"", capture_output=True,
                          env=base_env(**env), cwd=str(cwd or TMP), timeout=300)
    return Run(proc)


def project_lf_bytes() -> bytes:
    return lf((P2B_DIR / "project.json").read_bytes())


def native_sha256() -> str:
    record = json.loads((P2B_DIR / "record.json").read_text(encoding="utf-8"))
    return record["native_project"]["sha256"]


class EnvelopeTest(unittest.TestCase):
    def assert_ok(self, run: Run) -> dict[str, Any]:
        self.assertEqual(run.code, 0, run.stdout[:2000])
        doc = run.doc
        self.assertTrue(doc["ok"])
        self.assertEqual(doc["snapshot"]["id"], SNAPSHOT)
        self.assertTrue(run.stdout.endswith(b"\n") and not run.stdout.endswith(b"\n\n"))
        self.assertNotIn(b"\r", run.stdout)
        if "bounds" in doc:
            self.assertEqual(doc["bounds"]["output_bytes"], len(run.stdout))
            self.assertLessEqual(len(run.stdout), doc["bounds"]["max_output_bytes"])
        return doc

    def assert_error(self, run: Run, code: str) -> dict[str, Any]:
        doc = run.doc
        self.assertFalse(doc["ok"], doc)
        self.assertEqual(doc["error"]["code"], code, doc)
        self.assertEqual(set(doc["error"]), {"code", "message", "hint", "details"})
        self.assertEqual(run.code, EXIT_CODES[code])
        self.assertEqual(run.stderr, b"")
        return doc["error"]

    def test_info_envelope(self) -> None:
        doc = self.assert_ok(run_cli("info"))
        self.assertEqual(doc["result"]["meta"]["snapshot_id"], SNAPSHOT)
        self.assertEqual(doc["result"]["index"]["source"], "argument")
        self.assertEqual(Path(doc["result"]["index"]["path"]), INDEX.resolve())

    def test_global_options_before_or_after_command(self) -> None:
        after = run_cli("info", "--index", str(INDEX), index=False)
        before = run_cli("--index", str(INDEX), "info", index=False)
        self.assertEqual(after.stdout, before.stdout)
        env = run_cli("info", index=False, OS_OPTICS_LENS_INDEX=str(INDEX))
        self.assertEqual(self.assert_ok(env)["result"]["index"]["source"], "env")

    def test_pretty_output_is_the_same_document(self) -> None:
        compact = run_cli("facets", "--facet", "design_type")
        pretty = run_cli("facets", "--facet", "design_type", "--pretty")
        self.assertEqual(compact.doc, json.loads(pretty.stdout))
        self.assertIn(b"\n  ", pretty.stdout)

    def test_error_classes_and_exit_codes(self) -> None:
        cases = [
            (("search", '{"mode": "specification", "constraints": [], "bogus": 1}'), "INVALID_REQUEST"),
            (("facets", "--facet", "no_such_facet"), "UNKNOWN_FACET"),
            (("facets", "--filter", "design_type=zzzz_unknown"), "UNKNOWN_FACET_VALUE"),
            (("facets", "--filter", "design_type=objective"), "AMBIGUOUS_TERM"),
            (("fields", "--fact", "first_order.no_such_key_mm"), "UNKNOWN_FIELD"),
            (("search", "--mode", "specification", "--where", "first_order.effective_focal_length_mm approx 18"),
             "UNSUPPORTED_OPERATOR"),
            (("search", "--mode", "starting_point", "--cursor", "not-a-cursor"), "CURSOR_INVALID"),
            (("inspect", P2B, "--prescription", "full", "--max-output-bytes", "2048"), "OUTPUT_LIMIT_EXCEEDED"),
            (("inspect", "no-such-record"), "RECORD_NOT_FOUND"),
            (("inspect", P2B, "--revision", "999"), "REVISION_NOT_AVAILABLE"),
            (("inspect", P2B, "--configuration", "Tele"), "CONFIGURATION_NOT_FOUND"),
            (("inspect", P2B, "--evidence", "no_such_section"), "EVIDENCE_SECTION_NOT_FOUND"),
        ]
        for argv, code in cases:
            with self.subTest(code=code):
                error = self.assert_error(run_cli(*argv), code)
                self.assertTrue(error["message"])
        error = self.assert_error(run_cli("inspect", "yutong-2025-mirrorless-18-p2c"), "RECORD_NOT_FOUND")
        self.assertIn(P2B, error["details"]["did_you_mean"])
        error = self.assert_error(run_cli("inspect", P2B, "--revision", "999"), "REVISION_NOT_AVAILABLE")
        self.assertEqual(error["details"]["available_revision"], CASE["record"]["revision"])

    def cursor_with(self, **changes: str) -> str:
        # Start with the CLI's own cursor and change only the field under test.
        doc = self.assert_ok(run_cli("search", "--mode", "starting_point", "--limit", "1"))
        cursor = doc["result"]["page"]["next_cursor"]
        self.assertIsInstance(cursor, str)
        data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        data.update(changes)
        return base64.urlsafe_b64encode(json.dumps(data).encode("ascii")).rstrip(b"=").decode("ascii")

    def test_cursor_from_another_snapshot(self) -> None:
        stale = self.cursor_with(s="0" * 16)
        error = self.assert_error(run_cli("search", "--mode", "starting_point", "--cursor", stale),
                                  "SNAPSHOT_MISMATCH")
        self.assertEqual(error["details"]["snapshot"], SNAPSHOT)
        mismatch = self.cursor_with(q="f" * 16)
        self.assert_error(run_cli("search", "--mode", "starting_point", "--cursor", mismatch), "CURSOR_MISMATCH")

    def test_explain_request_rejects_a_bad_cursor(self) -> None:
        for cursor, code in (("garbage", "CURSOR_INVALID"),
                             (self.cursor_with(s="0" * 16), "SNAPSHOT_MISMATCH"),
                             (self.cursor_with(q="f" * 16), "CURSOR_MISMATCH")):
            with self.subTest(code=code):
                explained = run_cli("search", "--mode", "starting_point", "--explain-request", "--cursor", cursor)
                searched = run_cli("search", "--mode", "starting_point", "--cursor", cursor)
                self.assertEqual(self.assert_error(explained, code)["details"]["pointer"], "/cursor")
                self.assert_error(searched, code)

    def test_unsupported_operator_detail_matches_help(self) -> None:
        error = self.assert_error(run_cli("search", "--mode", "specification", "--where",
                                          "aperture.stop_opening_diameter_mm zz 2"), "UNSUPPORTED_OPERATOR")
        self.assertIn("lte", error["details"]["supported"])
        row = next(line for line in cli.help_text("errors").decode("ascii").splitlines()
                   if line.startswith("| `UNSUPPORTED_OPERATOR`"))
        self.assertIn("details.supported", row)
        self.assertNotIn("details.allowed", row)

    def test_index_unavailable(self) -> None:
        missing = TMP / "missing.sqlite"
        error = self.assert_error(run_cli("info", "--index", str(missing), index=False), "INDEX_UNAVAILABLE")
        self.assertEqual(error["details"]["tried"][0]["source"], "argument")
        self.assertIn("index build --corpus", error["hint"])
        not_an_index = TMP / "not-an-index.sqlite"
        not_an_index.write_bytes(b"plain text, not SQLite")
        error = self.assert_error(run_cli("facets", "--index", str(not_an_index), index=False), "INDEX_UNAVAILABLE")
        self.assertIn("problem", error["details"]["tried"][0])


class UsageErrorTest(EnvelopeTest):
    def test_argparse_errors_are_json(self) -> None:
        for argv in (("facets", "--bogus"), (), ("bogus-command",), ("facets", "--limit", "abc"),
                     ("project", P2B, "--out", "x.json", "--stdout"), ("search", "--detail", "verbose"),
                     ("index",), ("compare",)):
            with self.subTest(argv=argv):
                error = self.assert_error(run_cli(*argv), "INVALID_REQUEST")
                self.assertTrue(error["details"]["argv_error"])
                self.assertTrue(error["details"]["usage"].startswith("usage: os-optics-lens"))

    def test_out_of_range_options_are_rejected_not_clamped(self) -> None:
        error = self.assert_error(run_cli("search", "--mode", "starting_point", "--limit", "51"), "INVALID_REQUEST")
        self.assertEqual(error["details"]["allowed"], {"min": 1, "max": 50})
        error = self.assert_error(run_cli("facets", "--max-output-bytes", "100"), "INVALID_REQUEST")
        self.assertEqual(error["details"]["allowed"], {"min": 2048, "max": 262144})

    def test_help_is_text_with_examples(self) -> None:
        top = run_cli("--help", index=False, PYTHONIOENCODING="ascii")
        self.assertEqual(top.code, 0)
        text = top.stdout.decode("ascii")
        self.assertIn("os-optics-lens help quickstart", text)
        commands = ["info", "facets", "fields", "search", "compare", "inspect", "project", "index", "help"]
        for command in commands:
            self.assertIn(command, text)
        for argv in [[c] for c in commands] + [["index", "build"], ["index", "validate"], ["index", "dump"]]:
            with self.subTest(argv=argv):
                result = run_cli(*argv, "--help", index=False)
                self.assertEqual(result.code, 0)
                # argparse help is console text (CRLF on Windows); JSON output never is.
                self.assertIn("examples:\n  os-optics-lens", result.stdout.decode("ascii").replace("\r\n", "\n"))

    def test_help_topics(self) -> None:
        listing = run_cli("help", index=False)
        self.assertEqual(listing.code, 0)
        for topic in cli.HELP_TOPICS:
            with self.subTest(topic=topic):
                result = run_cli("help", topic, index=False, PYTHONIOENCODING="cp936")
                self.assertEqual(result.code, 0)
                self.assertTrue(result.stdout.startswith(b"# "))
                result.stdout.decode("ascii")
                self.assertIn(topic.encode("ascii"), listing.stdout)
        shipped = {p.stem for p in (SRC / "os_optics_lens" / "help").glob("*.md")}
        self.assertEqual(shipped, set(cli.HELP_TOPICS))
        error = self.assert_error(run_cli("help", "semantic", index=False), "INVALID_REQUEST")
        self.assertIn("semantics", error["details"]["did_you_mean"])

    def test_help_facets_names_every_facet(self) -> None:
        text = cli.help_text("facets").decode("ascii")
        for facet in load_vocabulary().facet_names():
            self.assertIn(f"`{facet}`", text)


class InputEncodingTest(EnvelopeTest):
    REQUEST = {"mode": "specification", "filters": {"record_ids": [P2B]},
               "constraints": [{"fact": "image_format.image_circle_diameter_mm", "op": "gte", "value": 43.27}]}

    def search_with_file(self, data: bytes) -> dict[str, Any]:
        path = TMP / "request.json"
        path.write_bytes(data)
        return self.assert_ok(run_cli("search", "--request", str(path)))

    def test_request_file_encodings(self) -> None:
        text = json.dumps(self.REQUEST, indent=2)
        plain = self.search_with_file(text.encode("utf-8"))
        self.assertEqual(plain["result"]["candidates"][0]["outcome"], "MATCH")
        for data in (b"\xef\xbb\xbf" + text.encode("utf-8"), b"\xff\xfe" + text.encode("utf-16-le"),
                     b"\xfe\xff" + text.encode("utf-16-be"), text.replace("\n", "\r\n").encode("utf-8")):
            with self.subTest(prefix=data[:2]):
                self.assertEqual(self.search_with_file(data), plain)
        stdin = self.assert_ok(run_cli("search", "-", stdin=b"\xff\xfe" + text.encode("utf-16-le")))
        self.assertEqual(stdin, plain)
        positional = self.assert_ok(run_cli("search", json.dumps(self.REQUEST)))
        self.assertEqual(positional, plain)

    def test_undecodable_and_invalid_request_files(self) -> None:
        path = TMP / "bad-request.json"
        path.write_bytes(b'{"mode": "\xff\xfe\xfd"}')
        self.assert_error(run_cli("search", "--request", str(path)), "INVALID_REQUEST")
        path.write_bytes(b'{"mode": "specification", "limit": NaN}')
        self.assert_error(run_cli("search", "--request", str(path)), "INVALID_REQUEST")
        error = self.assert_error(run_cli("search", "--request", str(TMP / "absent.json")), "INVALID_REQUEST")
        self.assertIn("not found", error["message"])

    def test_quote_stripped_positional_json_hint(self) -> None:
        error = self.assert_error(run_cli("search", "{mode:specification,constraints:[]}"), "INVALID_REQUEST")
        self.assertIn("--request FILE", error["hint"])
        self.assertIn("quotes", error["hint"])
        error = self.assert_error(run_cli("search", '{"mode": "specification",'), "INVALID_REQUEST")
        self.assertNotIn("quotes", error["hint"])

    def test_duplicate_keys_are_rejected(self) -> None:
        duplicate = b'{"mode": "starting_point", "mode": "specification"}'
        error = self.assert_error(run_cli("search", "-", stdin=duplicate), "INVALID_REQUEST")
        self.assertEqual(error["details"]["pointer"], "/mode")
        self.assertIn("duplicate key 'mode'", error["message"])
        request = TMP / "duplicate.json"
        request.write_bytes(b'{"mode": "specification", "constraints": [{"fact": "aperture.stop_opening_diameter_mm", '
                            b'"op": "lte", "value": 2, "op": "gte"}]}')
        error = self.assert_error(run_cli("search", "--request", str(request)), "INVALID_REQUEST")
        self.assertEqual(error["details"]["pointer"], "/constraints/0/op")

    def test_non_utf8_stdout(self) -> None:
        for encoding in ("cp936", "cp1252", "ascii"):
            with self.subTest(encoding=encoding):
                result = run_cli("inspect", P2B, "--sections", "field,performance", "--max-output-bytes", "262144",
                                 PYTHONIOENCODING=encoding, PYTHONUTF8="0")
                doc = self.assert_ok(result)
                self.assertEqual(doc["result"]["record_id"], P2B)
                self.assertIn(b"\\u00d7", result.stdout)  # the record's U+00D7 is escaped, not encoded


class SearchTest(EnvelopeTest):
    def test_fixture_search_matches_all_constraints(self) -> None:
        doc = self.assert_ok(run_cli("search", "--fixture", str(FIXTURE_PATH)))
        candidates = doc["result"]["candidates"]
        self.assertEqual([c["record_id"] for c in candidates], [P2B])
        candidate = candidates[0]
        self.assertEqual(candidate["revision"], CASE["record"]["revision"])
        self.assertEqual(candidate["outcome"], "MATCH")
        self.assertEqual(candidate["counts"]["MATCH"], len(CASE["constraints"]))
        self.assertEqual(candidate["counts"]["MATCH"], CASE["expected"]["confirmed_constraints"])
        for constraint_id, result in candidate["constraints"].items():
            outcome = result if isinstance(result, str) else result["outcome"]
            self.assertEqual(outcome, "MATCH", constraint_id)

    def test_quick_flags_build_the_request(self) -> None:
        doc = self.assert_ok(run_cli(
            "search", "--mode", "starting_point", "--explain-request",
            "--filter", "design_type=camera", "--filter", "design_type=microscope_objective",
            "--range", "first_order.effective_focal_length_mm=10:", "--spectral-span", "450:650",
            "--where", "design.conjugate eq INFINITE_TO_FINITE",
            "--where", "first_order.effective_focal_length_mm between_inclusive [10, 30]",
            "--target", "first_order.effective_focal_length_mm=24~15%",
            "--target", "first_order.total_track_mm=100~20",
            "--prefer", "design.powered_element_count", "--prefer", "first_order.total_track_mm:desc",
            "--include", "MATCH,UNCONFIRMED", "--hard-shortfall", "allow"))
        q = doc["result"]["normalized"]
        self.assertEqual(q["filters"]["facets"], {"design_type": ["camera_lens", "microscope_objective"]})
        self.assertEqual(q["filters"]["ranges"],
                         [{"fact": "first_order.effective_focal_length_mm", "min": 10, "max": None}])
        self.assertEqual(q["filters"]["spectral_span"], [450, 650])
        self.assertEqual([(c["fact"], c["op"], c["value"]) for c in q["constraints"]],
                         [("design.conjugate", "eq", "INFINITE_TO_FINITE"),
                          ("first_order.effective_focal_length_mm", "between_inclusive", [10, 30])])
        self.assertAlmostEqual(q["targets"][0]["relative_tolerance"], 0.15)
        self.assertEqual(q["targets"][1]["tolerance"], 20)
        self.assertEqual([p["direction"] for p in q["preferences"]], ["ascending", "descending"])
        self.assertEqual(q["include"], ["MATCH", "UNCONFIRMED"])
        self.assertEqual(q["hard_shortfall"], "allow")

    def test_quick_flag_errors(self) -> None:
        for argv in (("--where", "first_order.effective_focal_length_mm"),
                     ("--target", "first_order.effective_focal_length_mm=abc"),
                     ("--range", "first_order.effective_focal_length_mm=10"),
                     ("--prefer", "design.powered_element_count:sideways"),
                     ("--filter", "design_type")):
            with self.subTest(argv=argv):
                error = self.assert_error(run_cli("search", "--mode", "starting_point", *argv), "INVALID_REQUEST")
                self.assertEqual(error["details"]["flag"], argv[0])
        error = self.assert_error(run_cli("search"), "INVALID_REQUEST")
        self.assertIn("--request", error["message"])
        request = TMP / "single.json"
        request.write_text(json.dumps({"mode": "starting_point"}), encoding="utf-8")
        error = self.assert_error(run_cli("search", "--request", str(request), "--mode", "specification"),
                                  "INVALID_REQUEST")
        self.assertEqual(error["details"]["flags"], ["--mode"])
        batch = TMP / "batch.json"
        batch.write_text(json.dumps({"queries": [{"mode": "starting_point"}]}), encoding="utf-8")
        self.assert_error(run_cli("search", "--request", str(batch), "--limit", "2"), "INVALID_REQUEST")
        self.assert_error(run_cli("search", "--request", str(request), "--fixture", str(FIXTURE_PATH)),
                          "INVALID_REQUEST")

    def test_cursor_continuation_concatenates_pages(self) -> None:
        request = TMP / "paged.json"
        request.write_text(json.dumps({"mode": "starting_point",
                                       "targets": [{"fact": "first_order.effective_focal_length_mm", "target": 50}]}),
                           encoding="utf-8")
        def every_page(*options: str) -> tuple[list[str], int]:
            seen: list[str] = []
            cursor = None
            while True:
                extra = ["--cursor", cursor] if cursor else []
                page = self.assert_ok(run_cli("search", "--request", str(request), *options, *extra))
                seen += [c["record_id"] for c in page["result"]["candidates"]]
                cursor = page["result"]["page"]["next_cursor"]
                if cursor is None:
                    return seen, page["result"]["total"]["listed"]

        # The corpus grows with new batches and can exceed one page of the largest size.
        expected, listed = every_page("--limit", "50", "--max-output-bytes", "262144")
        self.assertEqual(len(expected), listed)
        self.assertGreater(len(expected), 15)
        self.assertEqual(every_page("--limit", "15")[0], expected)

    def test_batch_budget(self) -> None:
        request = TMP / "batch-budget.json"
        request.write_text(json.dumps({"max_output_bytes": 4096, "queries": [
            {"mode": "starting_point", "limit": 10},
            {"mode": "specification", "constraints": [{"fact": "first_order.effective_focal_length_mm",
                                                         "op": "lte", "value": 35}]},
            {"mode": "bogus"}]}), encoding="utf-8")
        doc = self.assert_ok(run_cli("search", "--request", str(request)))
        self.assertEqual([entry["ok"] for entry in doc["results"]], [True, True, False])
        self.assertEqual(doc["results"][2]["error"]["code"], "INVALID_REQUEST")
        self.assertTrue(doc["bounds"]["truncated"])


class ProjectTest(EnvelopeTest):
    def test_metadata_only(self) -> None:
        doc = self.assert_ok(run_cli("project", P2B))
        self.assertEqual(doc["result"]["sha256"], native_sha256())
        self.assertEqual(doc["result"]["size_bytes"], len(project_lf_bytes()))
        inspected = self.assert_ok(run_cli("inspect", P2B))
        self.assertEqual(doc["result"]["configurations"], inspected["result"]["configurations"])
        self.assertFalse({"path", "license", "attribution", "provenance"} & doc["result"].keys())

    def test_out_writes_exact_bytes_and_refuses_overwrite(self) -> None:
        target = TMP / "handoff" / "nested" / "p2b.project.json"
        doc = self.assert_ok(run_cli("project", P2B, "--out", str(target)))
        data = target.read_bytes()
        self.assertEqual(data, project_lf_bytes())
        self.assertEqual(hashlib.sha256(data).hexdigest(), native_sha256())
        self.assertEqual(doc["result"]["written_sha256"], native_sha256())
        self.assertEqual(Path(doc["result"]["path"]), target.absolute())
        self.assertTrue(Path(doc["result"]["path"]).is_absolute())

        target.write_bytes(b"edited by the agent")
        error = self.assert_error(run_cli("project", P2B, "--out", str(target)), "FILE_EXISTS")
        self.assertEqual(Path(error["details"]["path"]), target.absolute())
        self.assertEqual(target.read_bytes(), b"edited by the agent")
        self.assert_ok(run_cli("project", P2B, "--out", str(target), "--force"))
        self.assertEqual(target.read_bytes(), project_lf_bytes())
        self.assertEqual(sorted(p.name for p in target.parent.iterdir()), [target.name])
        self.assert_error(run_cli("project", P2B, "--force"), "INVALID_REQUEST")

    def test_relative_out_resolves_against_cwd(self) -> None:
        work = TMP / "cwd-project"
        work.mkdir(exist_ok=True)
        doc = self.assert_ok(run_cli("project", P2B, "--out", "rel/p2b.json", cwd=work))
        self.assertEqual(Path(doc["result"]["path"]), (work / "rel" / "p2b.json").absolute())
        self.assertEqual((work / "rel" / "p2b.json").read_bytes(), project_lf_bytes())

    def test_stdout_writes_raw_bytes_only(self) -> None:
        for encoding in (None, "cp936"):
            with self.subTest(encoding=encoding):
                env = {"PYTHONIOENCODING": encoding} if encoding else {}
                result = run_cli("project", P2B, "--stdout", **env)
                self.assertEqual(result.code, 0)
                self.assertEqual(result.stdout, project_lf_bytes())
                self.assertEqual(hashlib.sha256(result.stdout).hexdigest(), native_sha256())
        error = self.assert_error(run_cli("project", "no-such-record", "--stdout"), "RECORD_NOT_FOUND")
        self.assertTrue(error["message"])

    def test_stdout_reader_closing_early_is_silent(self) -> None:
        # `project --stdout | head -c 10`: the Project (tens of KB) outgrows the pipe buffer, the
        # reader closes, the write fails (BrokenPipeError; OSError EINVAL on Windows). The process
        # exits 1 with nothing on stderr: no traceback, no INTERNAL envelope.
        self.assertGreater(len(project_lf_bytes()), 70000)
        argv = [sys.executable, "-m", "os_optics_lens", "--index", str(INDEX), "project", P2B, "--stdout"]
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                env=base_env(), cwd=str(TMP))
        try:
            head = proc.stdout.read(10)
            proc.stdout.close()
            stderr = proc.stderr.read()
            code = proc.wait(timeout=120)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.stderr.close()
        self.assertEqual(head, project_lf_bytes()[:10])
        self.assertEqual(stderr, b"")
        self.assertEqual(code, 1)


    def test_budget_is_validated_before_any_write(self) -> None:
        error = self.assert_error(run_cli("project", P2B, "--stdout", "--max-output-bytes", "2048"),
                                  "INVALID_REQUEST")
        self.assertEqual(error["details"]["flag"], "--max-output-bytes")
        self.assert_error(run_cli("project", P2B, "--stdout", "--max-output-bytes", "1"), "INVALID_REQUEST")
        target = TMP / "budget" / "x.json"
        error = self.assert_error(run_cli("project", P2B, "--out", str(target), "--max-output-bytes", "1"),
                                  "INVALID_REQUEST")
        self.assertEqual(error["details"]["allowed"], {"min": 2048, "max": 262144})
        self.assertFalse(target.parent.exists())
        self.assert_ok(run_cli("project", P2B, "--out", str(target), "--max-output-bytes", "2048"))
        self.assertEqual(target.read_bytes(), project_lf_bytes())

    def test_out_filesystem_errors_name_the_path(self) -> None:
        work = TMP / "fs-errors"
        work.mkdir(exist_ok=True)
        (work / "afile.txt").write_text("hi", encoding="utf-8")
        cases = [(work / "afile.txt" / "p.json", "not a directory")]
        if os.name == "nt":
            cases.append((work / "bad?name.json", None))
        for target, reason in cases:
            with self.subTest(target=target.name):
                error = self.assert_error(run_cli("project", P2B, "--out", str(target)), "INVALID_REQUEST")
                self.assertEqual(Path(error["details"]["path"]), target.absolute())
                self.assertEqual(error["details"]["flag"], "--out")
                self.assertIn(str(target.absolute()), error["message"])
                if reason:
                    self.assertIn(reason, error["message"])
                self.assertNotIn("bug", error["hint"])
        self.assertEqual(sorted(p.name for p in work.iterdir()), ["afile.txt"])

    @unittest.skipUnless(os.name == "nt", "replacing a read-only file fails only on Windows")
    def test_read_only_out_with_force(self) -> None:
        target = TMP / "ro-project.json"
        target.write_bytes(b"x")
        os.chmod(target, 0o444)
        try:
            error = self.assert_error(run_cli("project", P2B, "--out", str(target), "--force"), "INVALID_REQUEST")
        finally:
            os.chmod(target, 0o666)
        self.assertIn("permission denied", error["message"])
        self.assertEqual(target.read_bytes(), b"x")
        self.assertEqual([p.name for p in TMP.glob("ro-project.json*")], ["ro-project.json"])

    def test_damaged_project_blob_writes_nothing(self) -> None:
        damaged = TMP / "damaged.sqlite"
        shutil.copy2(INDEX, damaged)
        with contextlib.closing(sqlite3.connect(damaged)) as conn:
            conn.execute("UPDATE projects SET content = CAST(substr(content, 1, length(content) - 2) || "
                         "'X' || char(10) AS BLOB) WHERE record_pk = (SELECT record_pk FROM records "
                         "WHERE record_id = ?)", (P2B,))
            conn.commit()
        good = TMP / "kept.json"
        good.write_bytes(b"previous good copy")
        error = self.assert_error(run_cli("--index", str(damaged), "project", P2B, "--out", str(good), "--force",
                                          index=False), "INDEX_UNAVAILABLE")
        self.assertEqual(good.read_bytes(), b"previous good copy")
        self.assertEqual(error["details"]["record_id"], P2B)
        self.assertEqual(error["details"]["sha256"], native_sha256())
        self.assertNotEqual(error["details"]["actual_sha256"], native_sha256())
        self.assertIn("damaged", error["details"]["tried"][0]["problem"])
        fresh = TMP / "damaged-out" / "p.json"
        self.assert_error(run_cli("--index", str(damaged), "project", P2B, "--out", str(fresh), index=False),
                          "INDEX_UNAVAILABLE")
        self.assertFalse(fresh.exists())
        stdout = run_cli("--index", str(damaged), "project", P2B, "--stdout", index=False)
        self.assert_error(stdout, "INDEX_UNAVAILABLE")
        self.assertTrue(stdout.stdout.startswith(b'{"ok":false'))
        # Other records in the same index still hand off.
        other = run_cli("--index", str(damaged), "project", "portrait-85-sonnar-eight", "--stdout", index=False)
        self.assertEqual(other.code, 0)
        self.assertFalse(other.stdout.startswith(b'{"ok"'))


class ErrorBoundTest(EnvelopeTest):
    """Error envelopes are bounded documents too (spec section 7, amendment 10.13)."""

    def test_echoed_request_values_stay_within_budget(self) -> None:
        requests = {
            "mode": {"mode": "x" * 300000},
            "key": {"mode": "specification", "bogus_" + "k" * 300000: 1},
            "op": {"mode": "specification", "constraints": [{"fact": "a.b", "op": "o" * 300000, "value": 1}]},
            "facet": {"mode": "starting_point", "filters": {"facets": {"application": ["y" * 300000]}}},
        }
        for name, request in requests.items():
            path = TMP / f"huge-{name}.json"
            path.write_text(json.dumps(request), encoding="utf-8")
            for budget in (None, "4096"):
                with self.subTest(case=name, budget=budget):
                    args = ["search", "--request", str(path)] + (["--max-output-bytes", budget] if budget else [])
                    result = run_cli(*args)
                    self.assertLessEqual(len(result.stdout), int(budget or output.DEFAULT_MAX_OUTPUT_BYTES))
                    error = result.doc["error"]
                    self.assertIn(error["code"], ("INVALID_REQUEST", "UNSUPPORTED_OPERATOR", "UNKNOWN_FACET_VALUE"))
                    self.assertEqual(result.code, 2)
                    self.assertEqual(result.stderr, b"")
        request_budget = TMP / "huge-mode-budget.json"
        request_budget.write_text(json.dumps({"mode": "x" * 300000, "max_output_bytes": 2048}), encoding="utf-8")
        self.assertLessEqual(len(run_cli("search", "--request", str(request_budget)).stdout), 2048)
        long_value = "wide angle lens for " * 1000
        result = run_cli("facets", "--filter", f"application={long_value}", "--max-output-bytes", "2048")
        self.assert_error(result, "UNKNOWN_FACET_VALUE")
        self.assertLessEqual(len(result.stdout), 2048)

    def test_integer_beyond_double_range_is_invalid_request(self) -> None:
        huge = "1" + "0" * 400
        for request, pointer in (
            ('{"mode":"specification","constraints":[{"fact":"first_order.effective_focal_length_mm",'
             '"op":"lte","value":' + huge + '}]}', "/constraints/0/value"),
            ('{"mode":"starting_point","targets":[{"fact":"first_order.effective_focal_length_mm",'
             '"target":20,"tolerance":' + huge + '}]}', "/targets/0/tolerance"),
        ):
            with self.subTest(pointer=pointer):
                error = self.assert_error(run_cli("search", request), "INVALID_REQUEST")
                self.assertEqual(error["details"]["pointer"], pointer)

    def test_failed_build_envelope_stays_within_budget(self) -> None:
        corpus = TMP / "corpus-oversized-finding"
        shutil.copytree(ROOT / "records", corpus / "records")
        shutil.copy2(ROOT / "LICENSE", corpus / "LICENSE")
        shutil.copytree(ROOT / "licenses", corpus / "licenses")
        path = corpus / "records" / "yutong-cup" / P2B / "record.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["design"] = [record["design"]] * 300  # INVALID_FIELD whose `actual` is ~200 KB
        record["record_hash"] = record_hash(record)
        path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
        for argv in (("index", "build", "--corpus", str(corpus), "--out", str(TMP / "oversized.sqlite")),
                     ("index", "validate", "--corpus", str(corpus))):
            with self.subTest(command=argv[1]):
                result = run_cli(*argv, index=False)
                error = self.assert_error(result, "BUILD_FAILED")
                self.assertLessEqual(len(result.stdout), output.DEFAULT_MAX_OUTPUT_BYTES)
                self.assertTrue(error["details"]["truncated"])
                fields = {(f["code"], f["field"]) for f in error["details"]["findings"]}
                self.assertIn(("INVALID_FIELD", "design"), fields)


class IndexCommandTest(EnvelopeTest):
    def test_build_output_and_failed_build_keeps_previous_index(self) -> None:
        corpus = TMP / "corpus"
        shutil.copytree(ROOT / "records", corpus / "records")
        shutil.copy2(ROOT / "LICENSE", corpus / "LICENSE")
        shutil.copytree(ROOT / "licenses", corpus / "licenses")
        out = TMP / "built" / "lens.sqlite"
        result = run_cli("index", "build", "--corpus", str(corpus), "--out", str(out), index=False)
        self.assertEqual(result.code, 0, result.stdout[:2000])
        doc = result.doc
        built = doc["result"]
        self.assertEqual(Path(built["out"]), out.resolve())
        self.assertEqual(built["snapshot_id"], SNAPSHOT)
        self.assertEqual(doc["snapshot"]["id"], SNAPSHOT)
        self.assertEqual(built["use"], f"--index {out.resolve()}")
        self.assertEqual(built["records"], doc["snapshot"]["records"])
        self.assertIsInstance(built["warnings"], int)
        self.assertEqual(doc["bounds"]["output_bytes"], len(result.stdout))
        before = out.read_bytes()

        validated = run_cli("index", "validate", "--corpus", str(corpus), index=False)
        self.assertEqual(validated.code, 0)
        self.assertIsNone(validated.doc["result"]["out"])
        self.assertEqual(validated.doc["result"]["snapshot_id"], SNAPSHOT)

        broken = corpus / "records" / "yutong-cup" / P2B / "record.json"
        broken.write_bytes(b'{"schema": "opensci.lens.record",')
        failed = run_cli("index", "build", "--corpus", str(corpus), "--out", str(out), index=False)
        error = self.assert_error(failed, "BUILD_FAILED")
        self.assertEqual(failed.code, 6)
        files = {finding["file"] for finding in error["details"]["findings"]}
        self.assertIn(f"records/yutong-cup/{P2B}/record.json", files)
        self.assertEqual(error["details"]["findings"][0]["severity"], "error")
        self.assertLessEqual(len(error["details"]["findings"]), cli.MAX_LISTED_FINDINGS)
        self.assertEqual(out.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in out.parent.iterdir()), [out.name])
        self.assert_error(run_cli("index", "validate", "--corpus", str(corpus), index=False), "BUILD_FAILED")

    def test_build_onto_a_directory_is_invalid_request(self) -> None:
        adir = TMP / "build-into-dir"
        adir.mkdir(exist_ok=True)
        error = self.assert_error(run_cli("index", "build", "--corpus", str(ROOT), "--out", str(adir), index=False),
                                  "INVALID_REQUEST")
        self.assertEqual(Path(error["details"]["out"]), adir.resolve())
        self.assertEqual(list(adir.iterdir()), [])

    def test_build_without_records_dir(self) -> None:
        empty = TMP / "empty-corpus"
        empty.mkdir(exist_ok=True)
        self.assert_error(run_cli("index", "build", "--corpus", str(empty), "--out", str(TMP / "x.sqlite"),
                                  index=False), "INVALID_REQUEST")

    def test_dump(self) -> None:
        result = run_cli("index", "dump")
        self.assertEqual(result.code, 0)
        doc = result.doc
        self.assertEqual(doc["snapshot"]["id"], SNAPSHOT)
        self.assertIn("records", doc["result"]["tables"])
        self.assertEqual(run_cli("index", "dump").stdout, result.stdout)


class InProcessTest(unittest.TestCase):
    """Behavior that needs the process internals: the INTERNAL envelope and the parsers."""

    def capture(self, argv: list[str], **env: str) -> tuple[int, bytes, str]:
        stdout = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
        stderr = io.StringIO()
        with mock.patch.object(sys, "stdout", stdout), contextlib.redirect_stderr(stderr), \
                mock.patch.dict(os.environ, env, clear=False):
            code = cli.main(argv)
        stdout.flush()
        return code, stdout.buffer.getvalue(), stderr.getvalue()

    def test_internal_envelope_without_traceback(self) -> None:
        def boom(args: Any) -> Any:
            raise KeyError("unexpected")

        with mock.patch.object(cli, "_cmd_info", boom):
            os.environ.pop(cli.DEBUG_ENV, None)
            code, data, err = self.capture(["--index", str(INDEX), "info"])
            doc = json.loads(data)
            self.assertEqual(code, 1)
            self.assertEqual(doc["error"]["code"], "INTERNAL")
            self.assertEqual(doc["error"]["details"]["exception"], "KeyError")
            self.assertNotIn(b"Traceback", data)
            self.assertEqual(err, "")
            code, data, err = self.capture(["--index", str(INDEX), "info"], **{cli.DEBUG_ENV: "1"})
            self.assertEqual(code, 1)
            self.assertNotIn(b"Traceback", data)
            self.assertIn("Traceback", err)
            self.assertIn("KeyError", err)
        os.environ.pop(cli.DEBUG_ENV, None)

    def test_closed_pipe_on_windows_is_silent(self) -> None:
        # Windows reports a write to a pipe whose reader has gone as OSError EINVAL, not
        # BrokenPipeError; it must end silently too, without a second (INTERNAL) emission.
        class ClosedPipe(io.RawIOBase):
            def writable(self) -> bool:
                return True

            def write(self, data: Any) -> int:
                raise OSError(errno.EINVAL, "Invalid argument")

        class ClosedStdout:
            buffer = ClosedPipe()

            def write(self, text: str) -> int:
                raise OSError(errno.EINVAL, "Invalid argument")

            def flush(self) -> None:
                pass

            def fileno(self) -> int:
                raise io.UnsupportedOperation("fileno")

        for argv in (["--index", str(INDEX), "project", P2B, "--stdout"], ["--index", str(INDEX), "info"],
                     ["--index", str(INDEX), "inspect", "no-such-record"]):
            with self.subTest(argv=argv[2:]):
                stderr = io.StringIO()
                with mock.patch.object(sys, "stdout", ClosedStdout()), contextlib.redirect_stderr(stderr), \
                        mock.patch.dict(os.environ, {cli.DEBUG_ENV: "1"}):
                    code = cli.main(argv)
                self.assertEqual(code, 1)
                self.assertEqual(stderr.getvalue(), "")

    def test_lens_error_exit_code_is_preserved(self) -> None:
        def in_use(args: Any) -> Any:
            raise LensError("INDEX_IN_USE", "cannot replace the index")

        with mock.patch.object(cli, "_cmd_index_build", in_use):
            code, data, _ = self.capture(["index", "build"])
        self.assertEqual(code, 5)
        self.assertEqual(json.loads(data)["error"]["code"], "INDEX_IN_USE")

    def test_where_parsing(self) -> None:
        self.assertEqual(cli.parse_where("  design.conjugate   eq   INFINITE_TO_FINITE "),
                         {"fact": "design.conjugate", "op": "eq", "value": "INFINITE_TO_FINITE"})
        self.assertEqual(cli.parse_where("a.b in [1, 2.5, \"x y\"]"), {"fact": "a.b", "op": "in", "value": [1, 2.5, "x y"]})
        self.assertEqual(cli.parse_where('a.b eq "two words"'), {"fact": "a.b", "op": "eq", "value": "two words"})
        self.assertEqual(cli.parse_where("a.b eq two words"), {"fact": "a.b", "op": "eq", "value": "two words"})
        self.assertEqual(cli.parse_where("a.b gte 0.3"), {"fact": "a.b", "op": "gte", "value": 0.3})
        self.assertEqual(cli.parse_where("a.b has_source"), {"fact": "a.b", "op": "has_source"})
        with self.assertRaises(LensError):
            cli.parse_where("a.b gte")

    def test_target_and_prefer_parsing(self) -> None:
        self.assertEqual(cli.parse_target("x.y=24"), {"fact": "x.y", "target": 24})
        self.assertEqual(cli.parse_target("x.y=24~3"), {"fact": "x.y", "target": 24, "tolerance": 3})
        self.assertEqual(cli.parse_target("x.y=-2.5~10%"), {"fact": "x.y", "target": -2.5, "relative_tolerance": 0.1})
        for bad in ("x.y", "x.y=", "x.y=24~", "x.y=nan", "=24"):
            with self.subTest(bad=bad), self.assertRaises(LensError):
                cli.parse_target(bad)
        self.assertEqual(cli.parse_prefer("x.y"), {"fact": "x.y", "direction": "ascending"})
        self.assertEqual(cli.parse_prefer("x.y:DESC"), {"fact": "x.y", "direction": "descending"})

    def test_decode_document(self) -> None:
        text = '{"mode": "starting_point", "note": "×"}'
        for data in (text.encode("utf-8"), b"\xef\xbb\xbf" + text.encode("utf-8"),
                     b"\xff\xfe" + text.encode("utf-16-le"), b"\xfe\xff" + text.encode("utf-16-be"),
                     b"\xff\xfe\x00\x00" + text.encode("utf-32-le")):
            self.assertEqual(cli.decode_document(data, "test"), text)
        with self.assertRaises(LensError) as caught:
            cli.decode_document(text.encode("cp1252"), "test")  # U+00D7 as a lone 0xD7 byte
        self.assertEqual(caught.exception.code, "INVALID_REQUEST")


if __name__ == "__main__":
    unittest.main()

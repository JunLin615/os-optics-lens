"""Verify this tree together with the two phone intake branches, through the public CLI.

Usage::

    python scripts/verify_combined_intake.py --repo <clone> \\
        --phone-modules <ref> --phone-telephoto <ref> [--scratch DIR] [--keep] [--pretty]

A temporary corpus is assembled from this tree (``records/`` without the two phone collections,
``LICENSE``, ``licenses/`` and ``acceptance/``) plus ``records/phone-modules`` from the first ref
and ``records/phone-telephoto`` from the second. Ref files are read as raw git blobs, so they are
byte-exact; a path that would leave the temporary directory, a symlink or a submodule is refused.

Then ``python -m os_optics_lens`` (this tree's ``src/``) runs, each step one check of the JSON
report printed on stdout:

``index_validate`` and ``index_build``: strict, without ``--allow-unresolved-evidence``, 59 records;
``module_role_facet``: ``facets --facet module_role`` reports 6 main_camera, 5 ultrawide and
7 telephoto among 59 records; ``role_search.<role>``: a role-filtered search lists exactly that
many records; ``inspect_evidence``: ``inspect <id> --evidence prescription`` of every phone record
returns the ``prescription`` section of its extract, and every evidence reference is RESOLVED;
``yutong_fixture``: the acceptance fixture is MATCH with 16 matched constraints.

The report names the exact commit of each ref and of this tree. Exit status: 0 when every check
passes, 1 when a check fails, 2 for usage errors (bad ref, unsafe path, missing records). Only the
standard library and git are used; the temporary directory is removed unless ``--keep`` is given.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

CHECKOUT = Path(__file__).resolve().parents[1]
COLLECTIONS = ("phone-modules", "phone-telephoto")
FIXTURE = "acceptance/yutong-2025-mirrorless-problem-2.json"
EXPECTED_RECORDS = 59
EXPECTED_ROLES = {"main_camera": 6, "ultrawide": 5, "telephoto": 7}
EXPECTED_FIXTURE_MATCHES = 16
EVIDENCE_SECTION = "prescription"
MAX_OUTPUT_BYTES = "262144"
REGULAR_FILE_MODES = ("100644", "100755")
COMMAND_TIMEOUT_S = 600
REMOVED_ENV = ("PYTHONIOENCODING", "PYTHONUTF8", "OS_OPTICS_LENS_INDEX", "OS_OPTICS_LENS_DEBUG",
               "PYTHONHOME", "PYTHONSTARTUP")


class UsageProblem(Exception):
    """The inputs cannot be verified (exit status 2)."""


def git(repo: Path, *args: str) -> bytes:
    proc = subprocess.run(["git", "-C", str(repo), *args], stdin=subprocess.DEVNULL, capture_output=True,
                          timeout=COMMAND_TIMEOUT_S)
    if proc.returncode != 0:
        raise UsageProblem(f"git {' '.join(args)} failed in {repo}: "
                           + proc.stderr.decode("utf-8", "replace").strip()[-400:])
    return proc.stdout


def resolve_commit(repo: Path, ref: str) -> str:
    if ref.startswith("-"):
        raise UsageProblem(f"{ref!r} is not a git ref")
    return git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").decode("ascii").strip()


def safe_target(root: Path, path: str, prefix: str) -> Path:
    """Where the tree path `path` goes under `root`. Refused: a path outside `prefix`, absolute,
    with a ``..``, ``.`` or empty part, a backslash, colon or NUL, or resolving outside `root`."""
    parts = PurePosixPath(path).parts
    if (not path.startswith(prefix) or path.startswith("/") or any(c in path for c in "\\:\x00")
            or any(part in ("", ".", "..") for part in path.split("/")) or not parts):
        raise UsageProblem(f"refusing unsafe path {path!r} (expected a plain path under {prefix})")
    target = root.joinpath(*parts)
    if root.resolve() not in target.resolve().parents:
        raise UsageProblem(f"refusing path {path!r}: it leaves the temporary directory")
    return target


def tree_entries(listing: bytes) -> list[tuple[str, str, str, str]]:
    """(mode, type, object id, path) of `git ls-tree -r -z` output."""
    entries = []
    for record in listing.split(b"\0"):
        if not record:
            continue
        head, _, name = record.partition(b"\t")
        mode, kind, oid = head.decode("ascii").split()
        try:
            entries.append((mode, kind, oid, name.decode("utf-8")))
        except UnicodeDecodeError:
            raise UsageProblem(f"refusing a path that is not UTF-8: {name!r}") from None
    return entries


def extract_collection(repo: Path, commit: str, collection: str, corpus: Path) -> list[Path]:
    """Write ``records/<collection>`` of `commit` under `corpus`, byte-exact."""
    prefix = f"records/{collection}/"
    entries = tree_entries(git(repo, "ls-tree", "-r", "-z", "--full-tree", commit, "--", prefix.rstrip("/")))
    if not entries:
        raise UsageProblem(f"commit {commit} has no {prefix}")
    # Every entry is checked before anything is written.
    planned = []
    for mode, kind, oid, path in entries:
        if kind != "blob" or mode not in REGULAR_FILE_MODES:
            raise UsageProblem(f"refusing {path!r}: mode {mode} is not a regular file")
        planned.append((safe_target(corpus, path, prefix), oid))
    for target, oid in planned:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "xb") as handle:
            handle.write(git(repo, "cat-file", "blob", oid))
    return [target for target, _ in planned]


def assemble_corpus(repo: Path, commits: dict[str, str], corpus: Path) -> None:
    corpus.mkdir(parents=True)

    def skip_phone(directory: str, names: list[str]) -> list[str]:
        return [n for n in names if Path(directory) == CHECKOUT / "records" and n in COLLECTIONS]

    shutil.copytree(CHECKOUT / "records", corpus / "records", ignore=skip_phone)
    shutil.copyfile(CHECKOUT / "LICENSE", corpus / "LICENSE")
    for name in ("licenses", "acceptance"):
        shutil.copytree(CHECKOUT / name, corpus / name)
    for collection in COLLECTIONS:
        extract_collection(repo, commits[collection], collection, corpus)


def tree_state() -> dict[str, Any]:
    """The commit of this tree and whether it has uncommitted changes (null outside git)."""
    try:
        head = git(CHECKOUT, "rev-parse", "HEAD").decode("ascii").strip()
        dirty = bool(git(CHECKOUT, "status", "--porcelain").strip())
    except (UsageProblem, OSError):
        head, dirty = None, None
    return {"path": str(CHECKOUT), "commit": head, "uncommitted_changes": dirty}


class Verifier:
    def __init__(self, workdir: Path, python: str) -> None:
        self.workdir = workdir
        self.python = python
        self.checks: list[dict[str, Any]] = []
        env = {k: v for k, v in os.environ.items() if k not in REMOVED_ENV}
        env["PYTHONPATH"] = str(CHECKOUT / "src")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        self.env = env

    def cli(self, *args: str) -> tuple[Any, list[str]]:
        """One CLI command: (parsed response, problems)."""
        proc = subprocess.run([self.python, "-m", "os_optics_lens", *args], cwd=self.workdir, env=self.env,
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=COMMAND_TIMEOUT_S)
        try:
            doc = json.loads(proc.stdout.decode("ascii"))
        except ValueError:
            tail = (proc.stdout + proc.stderr).decode("utf-8", "replace")[-600:]
            return None, [f"exit {proc.returncode}; stdout is not one JSON document: {tail}"]
        if proc.returncode != 0 or not doc.get("ok"):
            error = doc.get("error") or {}
            findings = (error.get("details") or {}).get("findings") or []
            shown = [f"{f.get('code')} {f.get('file')} {f.get('field')}: {f.get('message')}" for f in findings[:20]]
            return doc, [f"exit {proc.returncode}: {error.get('code')}: {error.get('message')}", *shown]
        return doc, []

    def add(self, name: str, problems: list[str], **info: Any) -> None:
        entry: dict[str, Any] = {"name": name, "ok": not problems, **info}
        if problems:
            entry["problems"] = problems
        self.checks.append(entry)


def _expect(problems: list[str], what: str, actual: Any, expected: Any) -> None:
    if actual != expected:
        problems.append(f"{what} is {actual!r}, expected {expected!r}")


def run_checks(v: Verifier, corpus: Path) -> str | None:
    """Every check; returns the snapshot id of the strict build (None when it failed)."""
    index = v.workdir / "combined.sqlite"
    snapshots = []
    for name, args in (("index_validate", ["index", "validate", "--corpus", str(corpus)]),
                       ("index_build", ["index", "build", "--corpus", str(corpus), "--out", str(index)])):
        doc, problems = v.cli(*args)
        info: dict[str, Any] = {"command": " ".join(["os-optics-lens", *args[:2]])}
        if not problems:
            result = doc["result"]
            _expect(problems, "records", result.get("records"), EXPECTED_RECORDS)
            info.update(records=result.get("records"), warnings=result.get("warnings"),
                        warning_codes=sorted({f["code"] for f in result.get("findings", [])}))
            snapshots.append(result.get("snapshot_id"))
        v.add(name, problems, **info)
    if len(snapshots) != 2 or not index.is_file():
        return None
    if snapshots[0] != snapshots[1]:
        v.add("snapshot_agreement", [f"validate {snapshots[0]} != build {snapshots[1]}"])
    base = ["--index", str(index)]

    doc, problems = v.cli(*base, "facets", "--facet", "module_role")
    values: dict[str, int] = {}
    if not problems:
        values = {value: count for value, count in doc["result"]["facets"]["module_role"]["values"]}
        _expect(problems, "module_role counts", values, EXPECTED_ROLES)
        _expect(problems, "records", doc["result"]["records"], EXPECTED_RECORDS)
    v.add("module_role_facet", problems, values=values)

    phone_ids: list[str] = []
    for role, count in EXPECTED_ROLES.items():
        doc, problems = v.cli(*base, "search", "--mode", "starting_point", "--filter", f"module_role={role}",
                              "--limit", "50", "--max-output-bytes", MAX_OUTPUT_BYTES)
        ids: list[str] = []
        if not problems:
            result = doc["result"]
            ids = [c["record_id"] for c in result["candidates"]]
            _expect(problems, "listed", result["total"]["listed"], count)
            _expect(problems, "returned candidates", len(ids), count)
            _expect(problems, "candidate collections outside the phone batches",
                    sorted({c["collection"] for c in result["candidates"]} - set(COLLECTIONS)), [])
        phone_ids.extend(ids)
        v.add(f"role_search.{role}", problems, record_ids=ids)

    problems = []
    verified = 0
    expected_total = sum(EXPECTED_ROLES.values())
    _expect(problems, "distinct phone records found by role", len(set(phone_ids)), expected_total)
    for record_id in sorted(set(phone_ids)):
        found = list(corpus.glob(f"records/phone-*/{record_id}/record.json"))
        if len(found) != 1:
            problems.append(f"{record_id}: no single phone record.json")
            continue
        record = json.loads(found[0].read_bytes())
        path = record["collection"]["evidence_extract"]["path"]
        extract = json.loads(safe_target(corpus, path, "records/phone-").read_bytes())
        doc, own = v.cli(*base, "inspect", record_id, "--evidence", EVIDENCE_SECTION,
                         "--max-output-bytes", MAX_OUTPUT_BYTES)
        if not own:
            result = doc["result"]
            returned = {p: entry["sections"] for p, entry in result["evidence_sections"].items()}
            section = (extract.get("sections") or {}).get(EVIDENCE_SECTION)
            if not section or returned != {path: {EVIDENCE_SECTION: section}}:
                own.append(f"inspect did not return the {EVIDENCE_SECTION} section of {path}")
            statuses = sorted({ref["status"] for ref in result["evidence"]["refs"]
                               if str(ref["path"]).startswith("records/")})
            _expect(own, "evidence reference statuses", statuses, ["RESOLVED"])
            _expect(own, "extract record_id", extract.get("record_id"), record_id)
        problems.extend(f"{record_id}: {p}" for p in own)
        verified += not own
    _expect(problems, "records with verified prescription evidence", verified, expected_total)
    v.add("inspect_evidence", problems, section=EVIDENCE_SECTION, records_verified=verified)

    fixture_path = corpus / FIXTURE
    fixture = json.loads(fixture_path.read_bytes())
    doc, problems = v.cli(*base, "search", "--fixture", str(fixture_path),
                          "--max-output-bytes", MAX_OUTPUT_BYTES)
    info = {}
    if not problems:
        matching = [c for c in doc["result"]["candidates"] if c["record_id"] == fixture["record"]["id"]]
        _expect(problems, "fixture candidates", len(matching), 1)
        if matching:
            info = {"outcome": matching[0]["outcome"], "counts": matching[0]["counts"]}
            _expect(problems, "outcome", info["outcome"], "MATCH")
            _expect(problems, "matched constraints", info["counts"].get("MATCH"), EXPECTED_FIXTURE_MATCHES)
    v.add("yutong_fixture", problems, **info)
    return snapshots[1]


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify this tree together with the phone-modules and phone-telephoto refs through the "
                    "public CLI. Prints a JSON report; exits 0 only if every check passes.")
    parser.add_argument("--repo", required=True, help="git repository (clone or worktree) that holds both refs")
    parser.add_argument("--phone-modules", required=True, metavar="REF", help="ref providing records/phone-modules")
    parser.add_argument("--phone-telephoto", required=True, metavar="REF",
                        help="ref providing records/phone-telephoto")
    parser.add_argument("--scratch", help="directory for the temporary corpus (default: the system temp)")
    parser.add_argument("--keep", action="store_true", help="keep the temporary directory")
    parser.add_argument("--python", default=sys.executable, help="interpreter that runs the CLI")
    parser.add_argument("--pretty", action="store_true", help="indent the JSON report")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    indent = 2 if args.pretty else None
    repo = Path(args.repo).resolve()
    refs = {"phone-modules": args.phone_modules, "phone-telephoto": args.phone_telephoto}
    workdir: Path | None = None
    report: dict[str, Any] = {"ok": False, "repo": str(repo), "tree": tree_state()}
    try:
        commits = {name: resolve_commit(repo, ref) for name, ref in refs.items()}
        report["refs"] = {name: {"ref": refs[name], "commit": commits[name]} for name in COLLECTIONS}
        if args.scratch:
            Path(args.scratch).mkdir(parents=True, exist_ok=True)
        workdir = Path(tempfile.mkdtemp(prefix="combined-intake-", dir=args.scratch)).resolve()
        corpus = workdir / "corpus"
        assemble_corpus(repo, commits, corpus)
        verifier = Verifier(workdir, args.python)
        snapshot = run_checks(verifier, corpus)
        report.update(snapshot_id=snapshot, checks=verifier.checks,
                      ok=snapshot is not None and all(check["ok"] for check in verifier.checks))
        status = 0 if report["ok"] else 1
    except (UsageProblem, OSError) as exc:
        report["error"] = str(exc)
        status = 2
    finally:
        if workdir is not None:
            if args.keep:
                report["workdir"] = str(workdir)
            else:
                shutil.rmtree(workdir, ignore_errors=True)
    print(json.dumps(report, indent=indent, ensure_ascii=True))
    return status


if __name__ == "__main__":
    sys.exit(main())

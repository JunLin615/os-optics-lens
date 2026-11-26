"""Verify a built os-optics-lens wheel in a clean, offline virtual environment (spec §8, §9.4, 10.20).

Usage::

    python scripts/verify_wheel.py dist/os_optics_lens-0.1.0-py3-none-any.whl --scratch D:/tmp/lens-verify
    python scripts/verify_wheel.py WHEEL --scratch DIR --dry-run     # inspect the wheel, print the plan

Steps (each one is a check in the JSON report printed on stdout):

1. ``wheel_contents``: the wheel holds ``os_optics_lens/_data/index.sqlite``, the bundled corpus under
   ``os_optics_lens/_data/corpus/`` (every bundled ``project.json`` matches its record's
   ``native_project.sha256``; no CRLF), the acceptance fixture and both license files in
   ``*.dist-info/licenses/``.
2. ``venv`` / ``install``: a fresh virtual environment in a new directory under ``--scratch`` (which
   must be outside this checkout), created with ``uv venv`` when uv is available, else
   ``python -m venv``; the wheel is installed with ``--no-index`` (no network, no dependencies).
3. Every following command runs from an empty working directory under the scratch directory, with
   ``PYTHONPATH`` pointing only at a ``sitecustomize.py`` that makes ``socket.socket.connect``,
   ``connect_ex``, ``socket.create_connection`` and ``socket.getaddrinfo`` raise, and with
   ``PYTHONIOENCODING``, ``PYTHONUTF8``, ``OS_OPTICS_LENS_INDEX``, ``PYTHONHOME`` and ``VIRTUAL_ENV``
   removed from the environment, so the bundled index is used and stdout is a pipe with the
   platform's default encoding:
   ``network_blocked`` (probe), ``import_location`` (the package imports from the venv, not the
   checkout), ``info`` (console script), ``python_m_info`` (``python -m os_optics_lens``, same
   snapshot), ``facets``, ``search`` (a starting-point request read from a file),
   ``inspect`` (``yutong-2025-mirrorless-18-p2b``, ASCII JSON on the piped stdout),
   ``inspect_without_provenance`` (LensLibrary Project first-order facts) and ``inspect_provenance``
   (the same record's direct source, attribution and redistribution terms), ``project_out``
   (``project --out``; the file's SHA-256 equals the record's ``native_project.sha256``),
   ``inspect_evidence`` (``inspect --evidence back_focus`` of the same record returns that section
   of its bundled evidence extract, with every evidence reference RESOLVED),
   ``search_fixture`` (acceptance item (1) of spec §9 against the bundled index and the bundled
   acceptance file, amendment 9.4: ``search --fixture`` returns the fixture's record at revision 4
   (the fixture's revision) as MATCH with counts.MATCH equal to the confirmed constraints and every
   constraint MATCH; ``[0, expected.cost_proxy]`` is in ``rank.key``; the interval-MTF and
   corner-relative-illumination facts have source status RESOLVED; and ``inspect --fields
   performance.mtf_field_minimum`` shows the 11-sample grid equal to the fixture's
   ``required_sampled_field_fractions``), and ``index_rebuild`` (``index build --corpus
   <site-packages>/os_optics_lens/_data/corpus``; its snapshot_id equals the one ``info`` reports).

Exit status: 0 when every check passes, 1 when any check fails, 2 for usage errors. Only the standard
library is used. Nothing is deleted: each run creates a new ``verify-*`` directory in ``--scratch``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Iterator

CHECKOUT = Path(__file__).resolve().parents[1]
PACKAGE = "os_optics_lens"
DATA = f"{PACKAGE}/_data"
CORPUS = f"{DATA}/corpus"
INSPECT_RECORD = "yutong-2025-mirrorless-18-p2b"
PROVENANCE_RECORD = "lenslibrary-528155"
FIXTURE = "acceptance/yutong-2025-mirrorless-problem-2.json"
EVIDENCE_PATH = f"records/yutong-cup/{INSPECT_RECORD}/evidence.json"
EVIDENCE_SECTION = "back_focus"
ACCEPTANCE_REVISION = 4
"""Acceptance item (1): the fixture's record is found at revision 4."""
RESOLVED_EVIDENCE_CONSTRAINTS = ("mtf_0_to_0p8", "corner_relative_illumination")
"""Acceptance item (1): constraints whose fact evidence must resolve (source.status RESOLVED)."""
SAMPLED_GRID_CONSTRAINT = "mtf_0_to_0p8"
SAMPLED_GRID_FACT = "performance.mtf_field_minimum"
SAMPLED_GRID_SIZE = 11
LICENSE_FILES = ("LICENSE", "licenses/LensLibrary-MIT.txt")
COMMAND_TIMEOUT_S = 600
REMOVED_ENV = ("PYTHONIOENCODING", "PYTHONUTF8", "OS_OPTICS_LENS_INDEX", "PYTHONHOME", "VIRTUAL_ENV",
               "PYTHONSTARTUP", "PYTHONSAFEPATH", "PYTHONNOUSERSITE")
NETWORK_BLOCK_MARKER = "os-optics-lens verify_wheel: network access is disabled"

SITECUSTOMIZE = f'''"""Installed by scripts/verify_wheel.py: every outgoing network call raises."""
import socket

_MESSAGE = {NETWORK_BLOCK_MARKER!r}


def _blocked(*args, **kwargs):
    raise OSError(_MESSAGE)


socket.socket.connect = _blocked
socket.socket.connect_ex = _blocked
socket.create_connection = _blocked
socket.getaddrinfo = _blocked
'''

NETWORK_PROBE = (
    "import socket\n"
    "try:\n"
    "    socket.create_connection(('127.0.0.1', 9), timeout=1)\n"
    "except OSError as exc:\n"
    "    print('BLOCKED' if str(exc) == " + repr(NETWORK_BLOCK_MARKER) + " else 'OTHER:' + str(exc))\n"
    "else:\n"
    "    print('CONNECTED')\n"
)

SEARCH_REQUEST = {
    "mode": "starting_point",
    "filters": {"facets": {"design_type": ["camera_lens"]}},
    "targets": [{"fact": "first_order.effective_focal_length_mm", "target": 50, "relative_tolerance": 0.2}],
    "limit": 3,
}


class Check:
    """One named verification step and its outcome."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.ok = False
        self.problems: list[str] = []
        self.info: dict[str, Any] = {}

    def fail(self, problem: str) -> None:
        self.problems.append(problem)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "ok": self.ok and not self.problems}
        out.update(self.info)
        if self.problems:
            out["problems"] = self.problems
        return out


class Report:
    """Ordered checks plus run metadata; `ok` is true only when every check passed."""

    def __init__(self, wheel: Path) -> None:
        self.wheel = wheel
        self.meta: dict[str, Any] = {"wheel": str(wheel)}
        self.checks: list[Check] = []

    def start(self, name: str) -> Check:
        check = Check(name)
        self.checks.append(check)
        return check

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.ok and not c.problems for c in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, **self.meta, "checks": [c.to_dict() for c in self.checks]}


# Helpers -----------------------------------------------------------------------------------------

def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_inside(path: Path, parent: Path) -> bool:
    path, parent = path.resolve(), parent.resolve()
    return path == parent or parent in path.parents


def walk(doc: Any) -> Iterator[tuple[str, Any]]:
    """Every (key, value) pair of every object nested in `doc`, depth first in document order."""
    if isinstance(doc, dict):
        for key, value in doc.items():
            yield key, value
            yield from walk(value)
    elif isinstance(doc, list):
        for item in doc:
            yield from walk(item)


def first(doc: Any, key: str) -> Any:
    """The first value stored under `key` anywhere in `doc`, else None."""
    for found_key, value in walk(doc):
        if found_key == key:
            return value
    return None


def envelope_snapshot(doc: Any) -> str | None:
    """The snapshot id of a response: `snapshot.id`, else the first `snapshot_id` anywhere."""
    if isinstance(doc, dict) and isinstance(doc.get("snapshot"), dict) and isinstance(doc["snapshot"].get("id"), str):
        return doc["snapshot"]["id"]
    value = first(doc, "snapshot_id")
    return value if isinstance(value, str) else None


def build_snapshot(doc: Any) -> str | None:
    """The snapshot id an `index build` response reports (its `snapshot_id`, else `snapshot.id`)."""
    value = first(doc, "snapshot_id")
    if isinstance(value, str):
        return value
    return envelope_snapshot(doc)


def candidates(doc: Any) -> list[dict[str, Any]]:
    """Search candidates of a single-query (`result.candidates`) or batch (`results[].result`) response."""
    found: list[dict[str, Any]] = []
    if not isinstance(doc, dict):
        return found
    results = [doc.get("result")] + [item.get("result") for item in doc.get("results") or [] if isinstance(item, dict)]
    for result in results:
        if isinstance(result, dict) and isinstance(result.get("candidates"), list):
            found.extend(c for c in result["candidates"] if isinstance(c, dict))
    return found


def constraint_outcome(value: Any) -> str | None:
    """Outcome of one compact/full constraint entry: the bare string or `{outcome: ...}`."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("outcome"), str):
        return value["outcome"]
    return None


def acceptance_failures(fixture: dict[str, Any], candidate: dict[str, Any], grid: Any) -> list[str]:
    """Every assertion of acceptance item (1) (spec §9, amendment 9.4) that fails, as messages.

    `candidate` is the fixture record's candidate from ``search --fixture`` (detail full) and
    `grid` the ``sampled_field_fractions`` that ``inspect --fields performance.mtf_field_minimum``
    reports for the record. An empty list means item (1) holds."""
    failures: list[str] = []
    expected = fixture.get("expected") or {}
    constraints = {c.get("id"): c for c in fixture.get("constraints") or [] if isinstance(c, dict)}
    confirmed = expected.get("confirmed_constraints")
    if isinstance(confirmed, int) and not isinstance(confirmed, bool):
        expected_matches = confirmed
    elif isinstance(confirmed, list):
        expected_matches = len(confirmed)
    else:
        expected_matches = len(constraints)
    fixture_revision = (fixture.get("record") or {}).get("revision")
    if candidate.get("revision") != ACCEPTANCE_REVISION or candidate.get("revision") != fixture_revision:
        failures.append(f"revision {candidate.get('revision')!r}, expected {ACCEPTANCE_REVISION} "
                        f"(fixture names {fixture_revision!r})")
    if candidate.get("outcome") != "MATCH":
        failures.append(f"outcome {candidate.get('outcome')!r}, expected MATCH")
    counts = candidate.get("counts") or {}
    if counts.get("MATCH") != expected_matches:
        failures.append(f"counts.MATCH {counts.get('MATCH')!r}, expected {expected_matches}")
    outcomes = {cid: constraint_outcome(v) for cid, v in (candidate.get("constraints") or {}).items()}
    not_matched = sorted(str(cid) for cid, outcome in outcomes.items() if outcome != "MATCH")
    if not_matched or len(outcomes) != expected_matches:
        failures.append(f"{len(outcomes)} constraint outcomes; not MATCH: {not_matched}")
    cost = expected.get("cost_proxy")
    key = (candidate.get("rank") or {}).get("key")
    if cost is None or not isinstance(key, list) or [0, cost] not in key:
        failures.append(f"rank.key {key!r} does not hold the comparable preference value [0, {cost!r}]")
    for cid in RESOLVED_EVIDENCE_CONSTRAINTS:
        entry = (candidate.get("constraints") or {}).get(cid)
        fact = entry.get("fact") if isinstance(entry, dict) else None
        source = fact.get("source") if isinstance(fact, dict) else None
        status = source.get("status") if isinstance(source, dict) else None
        if status != "RESOLVED":
            failures.append(f"constraint {cid}: evidence source status {status!r}, expected RESOLVED")
    required = (constraints.get(SAMPLED_GRID_CONSTRAINT) or {}).get("required_sampled_field_fractions")
    if not isinstance(grid, list) or len(grid) != SAMPLED_GRID_SIZE or grid != required:
        failures.append(f"{SAMPLED_GRID_FACT} sampled_field_fractions {grid!r}, expected the "
                        f"{SAMPLED_GRID_SIZE}-sample grid {required!r}")
    return failures


def evidence_failures(extract: dict[str, Any], doc: Any) -> list[str]:
    """What is wrong with an ``inspect --evidence`` response for the bundled extract."""
    result = doc.get("result") if isinstance(doc, dict) else None
    if not isinstance(result, dict):
        return ["inspect --evidence returned no result"]
    failures: list[str] = []
    section = (extract.get("sections") or {}).get(EVIDENCE_SECTION)
    entry = (result.get("evidence_sections") or {}).get(EVIDENCE_PATH)
    returned = entry.get("sections") if isinstance(entry, dict) else None
    if not section or returned != {EVIDENCE_SECTION: section}:
        failures.append(f"inspect --evidence did not return the {EVIDENCE_SECTION} content of {EVIDENCE_PATH}")
    statuses = sorted({ref.get("status") for ref in (result.get("evidence") or {}).get("refs") or []
                       if ref.get("path") == EVIDENCE_PATH})
    if statuses != ["RESOLVED"]:
        failures.append(f"evidence reference statuses {statuses!r}, expected ['RESOLVED']")
    return failures


class Runner:
    """Runs commands from an empty directory with network access blocked."""

    def __init__(self, workdir: Path, netblock_dir: Path) -> None:
        self.workdir = workdir
        env = {k: v for k, v in os.environ.items() if k not in REMOVED_ENV}
        env["PYTHONPATH"] = str(netblock_dir)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        self.env = env

    def run(self, argv: list[str]) -> tuple[int, bytes, bytes]:
        proc = subprocess.run(argv, cwd=self.workdir, env=self.env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=COMMAND_TIMEOUT_S)
        return proc.returncode, proc.stdout, proc.stderr

    def json_command(self, check: Check, argv: list[str], *, expect_exit: int = 0) -> Any:
        """Run `argv`, record it on `check`, and parse its stdout as one ASCII JSON document."""
        check.info["command"] = [Path(argv[0]).name, *argv[1:]]
        try:
            code, out, err = self.run(argv)
        except (OSError, subprocess.TimeoutExpired) as exc:
            check.fail(f"cannot run command: {exc}")
            return None
        check.info["exit_code"] = code
        check.info["stdout_bytes"] = len(out)
        if code != expect_exit:
            check.fail(f"exit code {code}, expected {expect_exit}")
        if err.strip():
            check.info["stderr_tail"] = err.decode("utf-8", "replace")[-600:]
        try:
            text = out.decode("ascii")
        except UnicodeDecodeError:
            check.fail("stdout is not ASCII JSON")
            text = out.decode("utf-8", "replace")
        try:
            doc = json.loads(text)
        except ValueError:
            check.fail("stdout is not one JSON document: " + text[:300])
            return None
        if isinstance(doc, dict) and doc.get("ok") is not True and expect_exit == 0:
            check.fail("response ok is not true: " + json.dumps(doc.get("error"))[:400])
        return doc


# Steps -------------------------------------------------------------------------------------------

def check_wheel_contents(report: Report) -> dict[str, Any]:
    """Static inspection of the wheel archive. Returns facts later steps compare against."""
    check = report.start("wheel_contents")
    facts: dict[str, Any] = {}
    try:
        archive = zipfile.ZipFile(report.wheel)
    except (OSError, zipfile.BadZipFile) as exc:
        check.fail(f"cannot open wheel: {exc}")
        return facts
    with archive:
        names = set(archive.namelist())
        index = f"{DATA}/index.sqlite"
        if index not in names:
            check.fail(f"{index} is missing")
        else:
            facts["index_bytes"] = archive.getinfo(index).file_size
        corpus = sorted(n for n in names if n.startswith(CORPUS + "/") and not n.endswith("/"))
        groups: dict[str, int] = {}
        for name in corpus:
            rel = name[len(CORPUS) + 1:]
            top = rel.split("/", 1)[0]
            groups[top] = groups.get(top, 0) + 1
            if b"\r\n" in archive.read(name):
                check.fail(f"{name} contains CRLF line endings")
        records = [n for n in corpus if n.endswith("/record.json")]
        for name in records:
            record = json.loads(archive.read(name))
            project_name = f"{CORPUS}/{record['native_project']['path']}"
            if project_name not in names:
                check.fail(f"{project_name} (native Project of {record.get('record_id')}) is missing")
            elif sha256(archive.read(project_name)) != record["native_project"]["sha256"]:
                check.fail(f"{project_name} does not match native_project.sha256")
            if record.get("record_id") == INSPECT_RECORD:
                facts["inspect_project_sha256"] = record["native_project"]["sha256"]
                facts["inspect_revision"] = record.get("record_revision")
            if record.get("record_id") == PROVENANCE_RECORD:
                collection = record["collection"]
                facts["direct_source"] = {
                    key: collection[key]
                    for key in ("source", "source_input", "source_revision", "attribution")
                }
                facts["redistribution"] = collection["redistribution"]
                facts["paraxial_efl_mm"] = record["first_order"]["engine_check"]["effective_focal_length_mm"]
        fixture = f"{CORPUS}/{FIXTURE}"
        if fixture not in names:
            check.fail(f"{fixture} is missing")
        else:
            facts["fixture"] = json.loads(archive.read(fixture))
        dist_info = sorted({n.split("/", 1)[0] for n in names if n.split("/", 1)[0].endswith(".dist-info")})
        for license_file in LICENSE_FILES:
            if not any(f"{d}/licenses/{license_file}" in names for d in dist_info):
                check.fail(f"*.dist-info/licenses/{license_file} is missing")
        if "inspect_project_sha256" not in facts:
            check.fail(f"record {INSPECT_RECORD} is not bundled")
        if "direct_source" not in facts:
            check.fail(f"record {PROVENANCE_RECORD} is not bundled")
        check.info.update({
            "wheel_bytes": report.wheel.stat().st_size,
            "entries": len(names),
            "index_bytes": facts.get("index_bytes"),
            "corpus_files": len(corpus),
            "corpus_files_by_top_level": dict(sorted(groups.items())),
            "records": len(records),
        })
    check.ok = True
    return facts


def find_uv(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    return shutil.which("uv")


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def console_script(venv: Path) -> Path:
    return venv / ("Scripts/os-optics-lens.exe" if os.name == "nt" else "bin/os-optics-lens")


def create_and_install(report: Report, run_dir: Path, python: str, uv: str | None) -> Path | None:
    """Create the venv and install the wheel offline. Returns the venv directory, or None on failure."""
    venv = run_dir / "venv"
    check = report.start("venv")
    env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME")}
    env["UV_OFFLINE"] = "1"
    env["PIP_NO_INDEX"] = "1"
    if uv:
        create = [uv, "venv", "--python", python, "--no-config", str(venv)]
    else:
        create = [python, "-m", "venv", str(venv)]
    check.info["command"] = create
    check.info["installer"] = "uv" if uv else "pip"
    proc = subprocess.run(create, cwd=run_dir, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          stdin=subprocess.DEVNULL, timeout=COMMAND_TIMEOUT_S)
    if proc.returncode != 0 or not venv_python(venv).is_file():
        check.fail(f"venv creation failed (exit {proc.returncode}): {proc.stderr.decode('utf-8', 'replace')[-600:]}")
        return None
    check.ok = True

    check = report.start("install")
    if uv:
        install = [uv, "pip", "install", "--no-config", "--offline", "--no-index", "--no-deps",
                   "--python", str(venv_python(venv)), str(report.wheel)]
    else:
        install = [str(venv_python(venv)), "-m", "pip", "install", "--no-index", "--no-deps",
                   "--disable-pip-version-check", str(report.wheel)]
    check.info["command"] = install
    proc = subprocess.run(install, cwd=run_dir, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          stdin=subprocess.DEVNULL, timeout=COMMAND_TIMEOUT_S)
    if proc.returncode != 0:
        check.fail(f"install failed (exit {proc.returncode}): {proc.stderr.decode('utf-8', 'replace')[-800:]}")
        return None
    if not console_script(venv).is_file():
        check.fail(f"console script {console_script(venv)} was not installed")
        return None
    check.ok = True
    return venv


def run_installed_checks(report: Report, run_dir: Path, venv: Path, facts: dict[str, Any]) -> None:
    """All checks that execute the installed package offline from an unrelated directory."""
    netblock = run_dir / "netblock"
    netblock.mkdir()
    (netblock / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")
    workdir = run_dir / "cwd"
    workdir.mkdir()
    runner = Runner(workdir, netblock)
    python = str(venv_python(venv))
    cli = str(console_script(venv))

    check = report.start("network_blocked")
    code, out, err = runner.run([python, "-c", NETWORK_PROBE])
    verdict = out.decode("utf-8", "replace").strip()
    check.info["probe"] = verdict
    if code != 0 or verdict != "BLOCKED":
        check.fail("sitecustomize.py did not block socket connections; refusing to run the remaining checks")
        return
    check.ok = True

    check = report.start("import_location")
    code, out, err = runner.run([python, "-c", f"import pathlib, {PACKAGE}; print(pathlib.Path({PACKAGE}.__file__).parent)"])
    package_dir = Path(out.decode("utf-8", "replace").strip()) if code == 0 else None
    check.info["package_dir"] = str(package_dir) if package_dir else None
    if package_dir is None:
        check.fail("cannot import the installed package: " + err.decode("utf-8", "replace")[-400:])
        return
    if is_inside(package_dir, CHECKOUT) or not is_inside(package_dir, venv):
        check.fail(f"{PACKAGE} imports from {package_dir}, not from the venv")
    check.ok = True

    check = report.start("info")
    doc = runner.json_command(check, [cli, "info"])
    snapshot = envelope_snapshot(doc)
    check.info["snapshot_id"] = snapshot
    if snapshot is None:
        check.fail("info reports no snapshot id")
    check.ok = True
    report.meta["snapshot_id"] = snapshot

    check = report.start("python_m_info")
    doc = runner.json_command(check, [python, "-m", PACKAGE, "info"])
    if envelope_snapshot(doc) != snapshot:
        check.fail(f"python -m reports snapshot {envelope_snapshot(doc)!r}, console script {snapshot!r}")
    check.ok = True

    check = report.start("facets")
    doc = runner.json_command(check, [cli, "facets"])
    result = doc.get("result") if isinstance(doc, dict) else None
    if not result:
        check.fail("facets returned no result")
    check.ok = True

    check = report.start("search")
    request_file = run_dir / "search-request.json"
    request_file.write_text(json.dumps(SEARCH_REQUEST), encoding="utf-8")
    doc = runner.json_command(check, [cli, "search", "--request", str(request_file)])
    found = candidates(doc)
    check.info["candidates"] = len(found)
    if not found:
        check.fail("search returned no candidates")
    check.ok = True

    check = report.start("inspect")
    doc = runner.json_command(check, [cli, "inspect", INSPECT_RECORD])
    if first(doc, "record_id") != INSPECT_RECORD:
        check.fail(f"inspect does not report record_id {INSPECT_RECORD}")
    revision = first(doc, "revision")
    check.info["revision"] = revision
    if facts.get("inspect_revision") is not None and revision != facts["inspect_revision"]:
        check.fail(f"inspect reports revision {revision!r}, the bundled record has {facts['inspect_revision']!r}")
    check.ok = True

    check = report.start("inspect_without_provenance")
    doc = runner.json_command(check, [cli, "inspect", PROVENANCE_RECORD, "--fields", "first_order",
                                      "--sections", "first_order"])
    result = doc.get("result") if isinstance(doc, dict) else None
    if not isinstance(result, dict) or result.get("record_id") != PROVENANCE_RECORD:
        check.fail(f"inspect does not report record_id {PROVENANCE_RECORD}")
    else:
        collection = result.get("collection")
        if collection != {"collection_id": "lenslibrary"}:
            check.fail("ordinary inspect must report only the collection ID in collection")
        first_order = (result.get("sections") or {}).get("first_order") or {}
        efl = first_order.get("effective_focal_length_mm") or {}
        if efl.get("basis") != "ENGINE_COMPUTED" or efl.get("value") != facts["paraxial_efl_mm"]:
            check.fail("ordinary inspect does not report the Project's engine-computed focal length")
        hidden = {"provenance", "license", "attribution", "redistribution", "conversion", "validation",
                  "source_input", "source_revision", "source_links", "source_reference", "original_publication",
                  "checks", "engine_check", "property_list_comparison", "prescription_dump_check"}
        payload = {key: value for key, value in result.items() if key != "next"}
        leaked = sorted({key for key, _ in walk(payload)} & hidden)
        if leaked:
            check.fail(f"ordinary inspect exposes provenance or intake keys: {leaked}")
    check.ok = True

    check = report.start("inspect_provenance")
    doc = runner.json_command(check, [cli, "inspect", PROVENANCE_RECORD, "--provenance"])
    result = doc.get("result") if isinstance(doc, dict) else None
    if not isinstance(result, dict) or result.get("record_id") != PROVENANCE_RECORD:
        check.fail(f"inspect --provenance does not report record_id {PROVENANCE_RECORD}")
    else:
        provenance = result.get("provenance")
        if not isinstance(provenance, dict):
            check.fail("inspect --provenance returns no provenance object")
        else:
            for key, expected in facts["direct_source"].items():
                if provenance.get(key) != expected:
                    check.fail(f"inspect --provenance {key} does not match the bundled record")
            license_info = provenance.get("license")
            if not isinstance(license_info, dict) or license_info.get("redistribution") != facts["redistribution"]:
                check.fail("inspect --provenance license.redistribution does not match the bundled record")
    check.ok = True

    check = report.start("project_out")
    target = run_dir / "handoff" / f"{INSPECT_RECORD}.project.json"
    doc = runner.json_command(check, [cli, "project", INSPECT_RECORD, "--out", str(target)])
    result = doc.get("result") if isinstance(doc, dict) else None
    if isinstance(result, dict) and {"provenance", "license", "attribution"} & result.keys():
        check.fail("project handoff exposes provenance")
    expected = facts.get("inspect_project_sha256")
    if not target.is_file():
        check.fail(f"project --out did not write {target}")
    else:
        written = sha256(target.read_bytes())
        check.info["sha256"] = written
        if written != expected:
            check.fail(f"written Project sha256 {written} != native_project.sha256 {expected}")
        for key in ("sha256", "written_sha256"):
            reported = first(doc, key)
            if reported is not None and reported != expected:
                check.fail(f"response {key} {reported} != native_project.sha256 {expected}")
    check.ok = True

    check = report.start("inspect_evidence")
    doc = runner.json_command(check, [cli, "inspect", INSPECT_RECORD, "--evidence", EVIDENCE_SECTION,
                                      "--max-output-bytes", "262144"])
    try:
        extract = json.loads((package_dir / "_data" / "corpus" / EVIDENCE_PATH).read_bytes())
    except (OSError, ValueError) as exc:
        check.fail(f"cannot read the bundled extract {EVIDENCE_PATH}: {exc}")
    else:
        check.info["section"] = EVIDENCE_SECTION
        for problem in evidence_failures(extract, doc):
            check.fail(problem)
    check.ok = True

    check = report.start("search_fixture")
    fixture_path = package_dir / "_data" / "corpus" / FIXTURE
    fixture = facts.get("fixture") or {}
    record_id = (fixture.get("record") or {}).get("id")
    doc = runner.json_command(check, [cli, "search", "--fixture", str(fixture_path)])
    matching = [c for c in candidates(doc) if c.get("record_id") == record_id]
    check.info["search_command"] = check.info.get("command")
    inspected = runner.json_command(check, [cli, "inspect", str(record_id), "--fields", SAMPLED_GRID_FACT])
    result = inspected.get("result") if isinstance(inspected, dict) else None
    fields = result.get("fields") if isinstance(result, dict) else None
    field = fields.get(SAMPLED_GRID_FACT) if isinstance(fields, dict) else None
    value = field.get("value") if isinstance(field, dict) else None
    grid = value.get("sampled_field_fractions") if isinstance(value, dict) else None
    if len(matching) != 1:
        check.fail(f"expected exactly one candidate for {record_id}, got {len(matching)}")
    else:
        candidate = matching[0]
        check.info.update({"outcome": candidate.get("outcome"), "counts": candidate.get("counts"),
                           "revision": candidate.get("revision"),
                           "rank_key": (candidate.get("rank") or {}).get("key"), "sampled_grid": grid})
        failures = acceptance_failures(fixture, candidate, grid)
        if failures:
            check.fail("; ".join(failures))
    check.ok = True

    check = report.start("index_rebuild")
    rebuilt = run_dir / "rebuilt.sqlite"
    corpus_dir = package_dir / "_data" / "corpus"
    doc = runner.json_command(check, [cli, "index", "build", "--corpus", str(corpus_dir), "--out", str(rebuilt)])
    rebuilt_snapshot = build_snapshot(doc)
    check.info["snapshot_id"] = rebuilt_snapshot
    if not rebuilt.is_file():
        check.fail(f"index build did not write {rebuilt}")
    if rebuilt_snapshot != snapshot:
        check.fail(f"rebuilt snapshot {rebuilt_snapshot!r} != bundled snapshot {snapshot!r}")
    check.ok = True


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install an os-optics-lens wheel into a fresh offline venv outside the checkout and "
                    "exercise the installed CLI. Prints a JSON report; exits 0 only if every check passes.",
        epilog="example: python scripts/verify_wheel.py dist/os_optics_lens-0.1.0-py3-none-any.whl "
               "--scratch D:/tmp/lens-verify",
    )
    parser.add_argument("wheel", help="path of the .whl file to verify")
    parser.add_argument("--scratch", required=True,
                        help="directory outside this checkout; each run creates a new verify-* directory in it")
    parser.add_argument("--python", default=sys.executable,
                        help="interpreter for the venv (default: the interpreter running this script)")
    parser.add_argument("--installer", choices=("auto", "uv", "pip"), default="auto",
                        help="auto uses uv when found (or given with --uv), else python -m venv + pip")
    parser.add_argument("--uv", help="path of the uv executable (default: uv on PATH)")
    parser.add_argument("--dry-run", action="store_true",
                        help="only inspect the wheel and print the plan; create nothing")
    parser.add_argument("--pretty", action="store_true", help="indent the JSON report")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    wheel = Path(args.wheel).resolve()
    scratch = Path(args.scratch).resolve()
    usage_problems = []
    if not wheel.is_file() or wheel.suffix != ".whl":
        usage_problems.append(f"{wheel} is not a .whl file")
    if is_inside(scratch, CHECKOUT):
        usage_problems.append(f"--scratch {scratch} must be outside the checkout {CHECKOUT}")
    uv = None
    if args.installer in ("auto", "uv"):
        uv = find_uv(args.uv)
        if args.installer == "uv" and uv is None:
            usage_problems.append("--installer uv: no uv executable found (pass --uv PATH)")
    if usage_problems:
        print(json.dumps({"ok": False, "usage_errors": usage_problems}, indent=2 if args.pretty else None))
        return 2

    report = Report(wheel)
    report.meta.update({"python": args.python, "installer": "uv" if uv else "pip", "checkout": str(CHECKOUT)})
    facts = check_wheel_contents(report)
    if args.dry_run:
        report.meta["dry_run"] = True
        report.meta["plan"] = [
            f"create {scratch}/verify-*/venv with {'uv venv' if uv else 'python -m venv'} ({args.python})",
            "install the wheel with --no-index --no-deps",
            "run network_blocked, import_location, info, python_m_info, facets, search, inspect, "
            "inspect_without_provenance, inspect_provenance, project_out, inspect_evidence, search_fixture, "
            "index_rebuild from an empty directory with sockets disabled",
        ]
    elif report.ok:
        scratch.mkdir(parents=True, exist_ok=True)
        run_dir = Path(tempfile.mkdtemp(prefix="verify-", dir=scratch))
        report.meta["run_dir"] = str(run_dir)
        venv = create_and_install(report, run_dir, args.python, uv)
        if venv is not None:
            run_installed_checks(report, run_dir, venv, facts)
    print(json.dumps(report.to_dict(), indent=2 if args.pretty else None, ensure_ascii=True))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())

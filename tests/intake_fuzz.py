"""Type fuzz of everything the builder reads: replace the value at a JSON path of a record, Project,
batch manifest or evidence extract with another JSON type, re-hash so the change is reached, and
require that validation and the build raise nothing but `LensError`, and that every query on a
successfully built index answers without an internal error.

This is a helper module (not a test): `test_intake_review.py` runs a representative subset, and the
full fuzz over real records is run by hand with a small driver that builds `FuzzCase`s.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from os_optics_lens import output, query  # noqa: E402
from os_optics_lens.build import build_index  # noqa: E402
from os_optics_lens.canonical import record_hash  # noqa: E402
from os_optics_lens.errors import LensError  # noqa: E402
from os_optics_lens.store import open_index  # noqa: E402

REPLACEMENTS: tuple[Any, ...] = (None, True, 1, 1.5, "x", [], ["x"], {}, {"a": 1})
LARGE = 262144


def json_paths(doc: Any, base: tuple[Any, ...] = ()) -> Iterator[tuple[Any, ...]]:
    """Every JSON path of `doc`, the root () included."""
    yield base
    if isinstance(doc, dict):
        for key, value in doc.items():
            yield from json_paths(value, base + (key,))
    elif isinstance(doc, list):
        for position, value in enumerate(doc):
            yield from json_paths(value, base + (position,))


def replace_at(doc: Any, path: tuple[Any, ...], value: Any) -> Any:
    if not path:
        return copy.deepcopy(value)
    out = copy.deepcopy(doc)
    node = out
    for step in path[:-1]:
        node = node[step]
    node[path[-1]] = copy.deepcopy(value)
    return out


def _encode(doc: Any) -> bytes:
    return (json.dumps(doc, ensure_ascii=False, indent=1) + "\n").encode("utf-8")


@dataclass
class FuzzCase:
    """A one-record corpus: `files` maps corpus-relative paths to parsed JSON documents, `record`
    is the record.json path, `texts` holds non-JSON files (licenses)."""

    name: str
    record: str
    files: dict[str, Any]
    texts: dict[str, bytes] = field(default_factory=lambda: {"LICENSE": b"MIT\nCopyright (c) 2026 JunLin615\n"})

    def write(self, root: Path, mutated: str | None = None, path: tuple[Any, ...] = (), value: Any = None) -> None:
        """Write the corpus with the value at `path` of file `mutated` replaced. The files are
        re-serialized, so every reference is first pointed at the bytes as written; a mutated
        Project or evidence file gets its references updated again, and the record is re-hashed
        unless the mutation targets the record hash itself."""
        docs = dict(self.files)
        record = copy.deepcopy(docs[self.record])
        for name, doc in docs.items():
            if name != self.record:
                _point_at(record, name, hashlib.sha256(_encode(doc)).hexdigest())
        if mutated == self.record:
            record = replace_at(record, path, value)
        elif mutated is not None:
            docs[mutated] = replace_at(docs[mutated], path, value)
            _point_at(record, mutated, hashlib.sha256(_encode(docs[mutated])).hexdigest())
        if isinstance(record, dict) and not (mutated == self.record and path == ("record_hash",)):
            record["record_hash"] = record_hash(record)
        payloads = {name: _encode(doc) for name, doc in docs.items() if name != self.record}
        payloads[self.record] = _encode(record)
        if root.exists():
            shutil.rmtree(root)
        for name, payload in {**self.texts, **payloads}.items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)


def _point_at(node: Any, path: str, digest: str) -> None:
    if isinstance(node, dict):
        if node.get("path") == path and "sha256" in node:
            node["sha256"] = digest
        for item in node.values():
            _point_at(item, path, digest)
    elif isinstance(node, list):
        for item in node:
            _point_at(item, path, digest)


def exercise(index_path: Path) -> list[str]:
    """Every query operation on a built index; returns the internal errors."""
    problems: list[str] = []

    def call(what: str, operation) -> Any:
        try:
            response = operation()
            if isinstance(response, dict):
                output.serialize(response)
                if '"code":"INTERNAL"' in output.dumps(response):
                    problems.append(f"{what}: INTERNAL batch entry")
            return response
        except LensError as error:
            if error.code == "INTERNAL":
                problems.append(f"{what}: INTERNAL {error.message}")
        except Exception:  # noqa: BLE001 - this is what the fuzz looks for
            problems.append(f"{what}: " + " | ".join(traceback.format_exc().strip().splitlines()[-3:]))
        return None

    with open_index(index_path) as index:
        call("meta", index.meta)
        call("build findings", index.findings)
        keys = index.fact_keys()
        call("facets", lambda: query.facets(index, describe=True, max_output_bytes=LARGE))
        call("fields", lambda: query.fields(index, limit=500, max_output_bytes=LARGE))
        examples: list[dict[str, Any]] = []
        for key in keys:
            doc = call(f"fields --fact {key}", lambda: query.fields(index, fact=key, max_output_bytes=LARGE))
            example = (doc or {}).get("result", {}).get("example_constraint")
            if example:
                examples.append({"mode": "specification", "constraints": [example], "include": list(query.OUTCOMES)})
        constraints = [
            {"fact": "first_order.effective_focal_length_mm", "op": "lte", "value": 1000},
            {"fact": "spectrum.continuous_coverage_nm", "op": "covers_interval", "value": [450, 650]},
            {"fact": "performance.mtf_field_minimum", "op": "gte", "value": 0.1, "requires_evidence_source": True,
             "conditions": {"frequency_lp_mm": 40, "spectrum": {"sampling_wavelengths_nm": [486, 587, 656]}},
             "required_sampled_field_fractions": [0, 0.4, 0.8]},
            {"fact": "performance.mtf", "op": "gte", "value": 0.1, "requires_evidence_source": True},
            {"fact": "performance.cra", "op": "lte", "value": 40, "select": {"field_fraction": 1}},
            {"fact": "analysis_evidence.ghost", "op": "has_sourced_method"},
            {"fact": "analysis_evidence.ghost", "op": "has_source"},
            {"scope": ["field"], "predicates": [{"path": "", "op": "ne", "value": 0}]},
            {"scope": ["design"], "predicates": [{"path": "", "op": "ne", "value": 0}]},
            {"scope": ["performance"], "metric": "mtf", "predicates": [{"path": "", "op": "ne", "value": 0}]},
        ]
        ranking = {"mode": "starting_point", "detail": "full",
                   "targets": [{"fact": "first_order.effective_focal_length_mm", "target": 50},
                               {"fact": "performance.mtf", "target": 0.5}],
                   "preferences": [{"fact": "first_order.total_track_mm", "direction": "ascending"},
                                   {"scope": ["cost_proxy"], "metric": "weighted_spherical_element_count",
                                    "direction": "ascending", "spherical_element_weight": 1}]}
        requests: list[dict[str, Any]] = [
            {"mode": "specification", "constraints": [item], "detail": "full", "include": list(query.OUTCOMES)}
            for item in constraints] + examples + [ranking]
        for position, request in enumerate(requests):
            call(f"search {position} {json.dumps(request)[:120]}",
                 lambda: query.run_request(index, request, max_output_bytes=LARGE))
        call("search compact", lambda: query.run_request(index, {"mode": "starting_point"}))
        call("facets filtered", lambda: query.facets(index, {"facets": {"design_type": ["camera_lens"]}}))
        for record_id in index.record_ids():
            row = index.get_record(record_id)
            ids = [c["configuration_id"] for c in index.configurations(row["record_pk"], with_view=False)]
            refs = [record_id if cid is None else f"{record_id}@{cid}" for cid in ids][:10]
            call("compare", lambda: query.compare(index, refs, max_output_bytes=LARGE))
            for start in range(0, len(keys), 40):
                call("compare fields", lambda: query.compare(index, refs, keys[start:start + 40],
                                                             max_output_bytes=LARGE))
            call("inspect", lambda: query.inspect(index, record_id, max_output_bytes=LARGE))
            for cid in [None, *[c for c in ids if c is not None]]:
                call(f"inspect evidence {cid}", lambda: query.inspect(
                    index, record_id, configuration=cid, evidence=["all"], max_output_bytes=LARGE))
                call(f"inspect prescription {cid}", lambda: query.inspect(
                    index, record_id, configuration=cid, prescription="summary", max_output_bytes=LARGE))
                view = index.configuration(row["record_pk"], cid)["view"]
                sections = sorted({key.split(".")[0] for key in keys})
                call(f"inspect fields {cid}", lambda: query.inspect(
                    index, record_id, configuration=cid, fields=sections or None, max_output_bytes=LARGE))
                call(f"inspect sections {cid}", lambda: query.inspect(
                    index, record_id, configuration=cid, sections=sorted(view), max_output_bytes=LARGE))
            call("project", lambda: query.project_response(index, record_id, max_output_bytes=LARGE))
            call("project bytes", lambda: query.project_bytes(index, record_id))
    return problems


def run_mutation(case: FuzzCase, workdir: Path, mutated: str, path: tuple[Any, ...], value: Any,
                 base_dump: str | None = None) -> tuple[str, list[str]]:
    """(status, problems) of one mutation. Status: `strict` (built strictly), `allowed` (built only
    with allow_unresolved_evidence), `rejected` (neither). Queries run on every built index unless
    its content equals the unmutated one (`base_dump`)."""
    root, out = workdir / "corpus", workdir / "index.sqlite"
    case.write(root, mutated, path, value)
    problems: list[str] = []
    for status, allow in (("strict", False), ("allowed", True)):
        try:
            build_index(root, out, allow_unresolved_evidence=allow)
        except LensError as error:
            if error.code != "BUILD_FAILED":
                problems.append(f"{status} build raised {error.code}: {error.message}")
            continue
        except Exception:  # noqa: BLE001
            problems.append(f"{status} build: " + " | ".join(traceback.format_exc().strip().splitlines()[-4:]))
            continue
        if base_dump is None or content_digest(out) != base_dump:
            problems.extend(exercise(out))
        return status, problems
    return "rejected", problems


def content_digest(index_path: Path) -> str:
    with open_index(index_path) as index:
        return index.meta()["content_digest"]


# A synthetic two-configuration 1.1 record --------------------------------------------------------

SYNTH_DIR = "records/synth/zoom"


def two_configuration_case() -> FuzzCase:
    """A 1.1 record with two configurations that carries every kind of member the builder indexes,
    its Project (with the members the prescription summary reads), a batch manifest and an
    evidence extract."""
    evidence_path = f"{SYNTH_DIR}/evidence.json"
    evidence = {"schema": "opensci.lens.evidence_extract", "record_id": "zoom", "schema_version": "1.0",
                "source": {"publication": "synthetic"},
                "sections": {"mtf": {"rows": [[0.0, 0.7]]}, "prescription": {"surfaces": [1, 2]}}}
    evidence_sha = hashlib.sha256(_encode(evidence)).hexdigest()
    project_path = f"{SYNTH_DIR}/project.json"
    project = {
        "schema": "opensci.project", "project_schema_version": "1",
        "configurations": [{"configuration_id": "Wide", "label": "wide end"},
                           {"configuration_id": "Tele", "name": "tele end"}],
        "optical_models": [{"model_id": "LENS", "model": {
            "model_kind": "ASSEMBLY", "members": [{"member_id": "L1", "model_id": "E1",
                                                   "pose": {"position_mm": [0, 0, 0]}}],
            "surfaces": [{"surface_id": "S1", "front_material_id": "AIR"}]}}],
        "model_materials": [{"material_id": "AIR"}], "model_coatings": [{"coating_id": "AR"}],
        "components": [{"optical_role": "LENS"}],
        "observations": [{"observation_id": "IMG", "aperture": {"kind": "CIRCLE"},
                          "pose": {"position_mm": [0, 0, 50]}}],
    }
    source = {"path": evidence_path, "sha256": evidence_sha, "section": "mtf"}
    entries = []
    for cid, focal, grid in (("Wide", 24.0, [0, 0.4, 0.8]), ("Tele", 70.0, [0, 0.2, 0.4, 0.6, 0.8])):
        entries.append({
            "configuration_id": cid, "label": cid, "conjugate": "INFINITE_TO_FINITE",
            "first_order": {"effective_focal_length_mm": {"value": focal, "basis": "SOURCE_STATED",
                                                          "conditions": {"wavelength_nm": 587.6}},
                            "total_track_mm": {"value": 90.0, "definition": "first vertex to image"}},
            "aperture": {"f_number": {"value": 2.8, "basis": "SOURCE_STATED", "system_aperture": True},
                         "stop_opening_diameter_mm": 8.5},
            "field": {"kind": "ANGULAR", "half_field_deg": 23.0, "field_points_deg": [0, 23.0],
                      "basis": "SOURCE_STATED", "definition": "half field",
                      "verification": {"state": "MATCHED", "wavelength_nm": 587.6}},
            "performance": [{"metric": "mtf_field_minimum", "basis": "DERIVED", "value": 0.7, "state": "STATED",
                             "eligible_for": "ANY", "conditions": {"frequency_lp_mm": 40,
                             "spectrum": {"sampling_wavelengths_nm": [486, 587, 656], "incident_weights": [1, 1, 1]}},
                             "sampled_field_fractions": grid, "condition": "minimum over the interval",
                             "limiting_sample": {"field_fraction": 0.8}, "source": dict(source)},
                            {"metric": "mtf", "value": 0.5, "frequency_lp_mm": 40, "source": dict(source)},
                            {"metric": "distortion", "min_percent": -1.5, "max_percent": 0.5,
                             "conditions": "at the full field"}],
        })
    record = {
        "schema": "opensci.lens.record", "record_schema_version": "1.1", "record_id": "zoom",
        "record_revision": 1, "title": "Synthetic zoom",
        "collection": {"collection_id": "synth", "source": "synthetic", "attribution": "nobody",
                       "redistribution": "MIT, see LICENSE",
                       "license": {"spdx": "MIT", "notice_path": "LICENSE",
                                   "copyright": "Copyright (c) 2026 JunLin615"},
                       "evidence_extract": {"path": evidence_path, "sha256": evidence_sha}},
        "design": {"lens_type": "camera", "design_family_id": "family-zoom", "design_scope": "COMPLETE_SYSTEM",
                   "configuration_count": 2, "module_role": "main_camera", "powered_element_count": 5,
                   "function_tags": ["imaging"], "application_tags": ["photography"], "structure_tags": ["zoom"],
                   "aperture_validity": {"state": "STOP_BLOCKING", "stop_blocks_light": True,
                                         "eligible_for_stop_dependent_evaluation": True,
                                         "satisfies_stop_dependent_constraints": True, "basis": "modelled"}},
        "native_project": {"path": project_path, "sha256": hashlib.sha256(_encode(project)).hexdigest(),
                           "engine_version": "0.14.0"},
        "spectrum": {"sampling_wavelengths_nm": [486.1, 587.6, 656.3], "incident_weights": [1, 1, 1],
                     "band_facets": ["visible"],
                     "sampling_span_nm": {"min_nm": 486.1, "max_nm": 656.3, "eligible_for": "DISCOVERY_ONLY"},
                     "continuous_coverage_nm": {"state": "CONFIRMED", "min_nm": 450, "max_nm": 700}},
        "image_format": {"format_facets": ["full_frame"], "image_circle_diameter_mm": 43.3, "basis": "STATED"},
        "cost_proxy": {"weighted_spherical_element_count": 5, "spherical_element_weight": 1, "scope": "all",
                       "basis": "DERIVED", "source": dict(source)},
        "analysis_evidence": {"ghost": {"status": "STUDIED", "methods": [{"name": "trace", "source": dict(source)}],
                                        "source": dict(source)}},
        "validation": {"paraxial": "MATCHED", "project_check": "ACCEPTED", "field": "MATCHED", "notes": ["ok"]},
        "record_hash_method": "SHA-256 over parsed JSON without record_hash and record_hash_method",
        "configurations": entries,
    }
    record["record_hash"] = record_hash(record)
    manifest = {"schema": "opensci.lens.batch", "batch_schema_version": "1.0", "batch_id": "synth-1",
                "collection": {"collection_id": "synth", "source": "synthetic"},
                "counts": {"inputs": 1, "accepted": 1}, "accepted_record_ids": ["zoom"],
                "inputs": [{"record_id": "zoom", "outcome": "ACCEPTED"}]}
    record_path = f"{SYNTH_DIR}/record.json"
    return FuzzCase("synthetic-zoom", record_path, {
        record_path: record, project_path: project, evidence_path: evidence,
        "records/synth/batch-synth-1.json": manifest})

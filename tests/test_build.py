"""Index lifecycle across rebuilds (section 9 `test_build`).

Findings, failed builds, INDEX_IN_USE and reproducibility are covered by test_build_foundation.py;
this file covers the corpus-growth path: adding a record and rebuilding changes the snapshot, the
new record is searchable, and a cursor from the old snapshot is refused with SNAPSHOT_MISMATCH.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from os_optics_lens import query
from os_optics_lens.build import build_index, dump_index
from os_optics_lens.canonical import lf, record_hash
from os_optics_lens.errors import LensError
from os_optics_lens.store import open_index

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "lenslibrary-1792917"
NEW_ID = "growth-test-1792917"
NEW_COLLECTION = "growth-test"
EFL = "first_order.effective_focal_length_mm"


class Rebuild(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "corpus"
        self.root.mkdir()
        for name in ("records", "licenses"):
            shutil.copytree(ROOT / name, self.root / name)
        shutil.copy2(ROOT / "LICENSE", self.root / "LICENSE")
        self.out = Path(self._tmp.name) / "index.sqlite"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def add_record(self) -> None:
        source_dir = self.root / "records" / "lenslibrary" / SOURCE
        target_dir = self.root / "records" / NEW_COLLECTION / NEW_ID
        target_dir.mkdir(parents=True)
        shutil.copy2(source_dir / "project.json", target_dir / "project.json")
        record = json.loads(lf((source_dir / "record.json").read_bytes()).decode("utf-8"))
        record["record_id"] = NEW_ID
        record["collection"]["collection_id"] = NEW_COLLECTION
        record["design"]["design_family_id"] = NEW_ID
        record["native_project"]["path"] = f"records/{NEW_COLLECTION}/{NEW_ID}/project.json"
        engine = record["first_order"]["engine_check"]
        record["first_order"]["effective_focal_length_mm"] = {
            "value": engine["effective_focal_length_mm"],
            "basis": "ENGINE_COMPUTED",
            "conditions": {"wavelength_nm": engine["wavelength_nm"]},
        }
        record["record_hash"] = record_hash(record)
        (target_dir / "record.json").write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n",
                                                encoding="utf-8")

    def test_add_record_then_rebuild(self) -> None:
        first = build_index(self.root, self.out)
        with open_index(self.out) as index:
            source = query.inspect(index, SOURCE, fields=[EFL])["result"]["fields"][EFL]
            target = source["value"]["value"]
            request = {"mode": "starting_point", "targets": [{"fact": EFL, "target": target}], "limit": 2}
            old = query.search(index, request)
            cursor = old["result"]["page"]["next_cursor"]
            self.assertIsNotNone(cursor)
            self.assertNotIn(NEW_ID, [c["record_id"] for c in old["result"]["candidates"]])
        old_dump = dump_index(self.out)

        self.add_record()
        second = build_index(self.root, self.out)
        self.assertNotEqual(first.snapshot_id, second.snapshot_id)
        self.assertEqual(second.records, first.records + 1)
        self.assertNotEqual(dump_index(self.out), old_dump)
        with open_index(self.out) as index:
            self.assertEqual(index.snapshot_id, second.snapshot_id)
            fresh = query.search(index, request)
            candidate = fresh["result"]["candidates"][0]
            self.assertEqual(candidate["record_id"], NEW_ID)
            self.assertEqual(candidate["shortfalls"][0]["deviation"], 0)
            self.assertEqual(fresh["snapshot"]["id"], second.snapshot_id)
            with self.assertRaises(LensError) as caught:
                query.search(index, dict(request, cursor=cursor))
            self.assertEqual(caught.exception.code, "SNAPSHOT_MISMATCH")
            self.assertEqual(caught.exception.exit_code, 4)
            facets = query.facets(index, None, ["collection"])["result"]["facets"]["collection"]["values"]
            self.assertIn([NEW_COLLECTION, 1], facets)

    def test_rebuild_of_unchanged_corpus_is_identical(self) -> None:
        first = build_index(self.root, self.out)
        before = self.out.read_bytes()
        second = build_index(self.root, self.out)
        self.assertEqual(first.snapshot_id, second.snapshot_id)
        self.assertEqual(self.out.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()

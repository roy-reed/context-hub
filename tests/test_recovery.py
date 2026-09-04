from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from context_hub import ContextHub

from tests.helpers import read_jsonl


class RecoveryContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="context-hub-recovery-")
        self.base = Path(self.temporary.name)
        self.hub = ContextHub(self.base / "data")
        self.hub.initialize()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_index_failure_keeps_durable_event_and_reindex_repairs(self) -> None:
        with patch.object(ContextHub, "_index_event", side_effect=sqlite3.OperationalError("forced")):
            result = self.hub.put(
                action="append",
                kind="fact",
                content="持久化优先的合成故障事件",
                source_type="test",
                source_ref="synthetic:index-failure",
                confirmed=True,
            )

        self.assertEqual(result["status"], "persisted")
        self.assertEqual(result["index_state"], "pending_reindex")
        self.assertEqual(len(read_jsonl(self.hub.events_path)), 1)
        self.assertFalse(self.hub.doctor()["ok"])

        repaired = self.hub.doctor(reindex=True)
        self.assertTrue(repaired["ok"], repaired)
        found = self.hub.search("持久化优先")
        self.assertEqual(found["items"][0]["ref"], f"event:{result['event_id']}")

    def test_doctor_detects_derived_tamper_and_rebuilds_exactly(self) -> None:
        event = self.hub.put(
            action="append",
            kind="status",
            content="派生索引篡改检测样本",
            source_type="test",
            source_ref="synthetic:tamper",
            confirmed=True,
        )
        with closing(sqlite3.connect(self.hub.index_path)) as connection:
            connection.execute("DELETE FROM event_fts WHERE event_id=?", (event["event_id"],))
            connection.commit()

        diagnostic = self.hub.doctor()
        self.assertFalse(diagnostic["ok"])
        self.assertTrue(any("FTS" in item for item in diagnostic["errors"]))
        repaired = self.hub.doctor(reindex=True)
        self.assertTrue(repaired["ok"], repaired)
        self.assertEqual(repaired["counts"]["events"], 1)
        self.assertEqual(repaired["counts"]["event_fts"], 1)

    def test_missing_index_is_reported_and_recreated(self) -> None:
        self.hub.put(
            action="append",
            kind="fact",
            content="缺失索引重建测试",
            source_type="test",
            source_ref="synthetic:missing-index",
            confirmed=True,
        )
        self.hub.remove_index_for_test()
        self.assertFalse(self.hub.doctor()["ok"])
        self.assertTrue(self.hub.doctor(reindex=True)["ok"])

    def test_backup_contains_only_authoritative_sources_and_hash_manifest(self) -> None:
        self.hub.put(
            action="append",
            kind="fact",
            content="备份测试数据",
            source_type="test",
            source_ref="synthetic:backup",
            confirmed=True,
        )
        result = self.hub.backup(self.base / "exports")
        backup = Path(result["backup"])
        self.assertEqual(hashlib.sha256(backup.read_bytes()).hexdigest(), result["sha256"])

        with zipfile.ZipFile(backup) as archive:
            names = set(archive.namelist())
            self.assertEqual(
                names,
                {"config.toml", "manifest.json", "memory/events.jsonl", "backup-manifest.json"},
            )
            manifest = json.loads(archive.read("backup-manifest.json"))
            for entry in manifest["entries"]:
                payload = archive.read(entry["path"])
                self.assertEqual(hashlib.sha256(payload).hexdigest(), entry["sha256"])
                self.assertEqual(len(payload), entry["bytes"])


if __name__ == "__main__":
    unittest.main()

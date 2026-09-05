from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
from contextlib import closing
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from context_hub import ContextHub
from context_hub.errors import ValidationError

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

    def test_initialize_rebuilds_malformed_derived_metadata(self) -> None:
        event = self.hub.put(
            action="append",
            kind="fact",
            content="派生元数据损坏后的自动恢复样本",
            source_type="test",
            source_ref="synthetic:malformed-meta",
            confirmed=True,
        )
        for key, value in (
            ("events_size", "not-an-integer"),
            ("events_mtime_ns", "-1"),
            ("events_sha256", "not-a-sha256"),
        ):
            with self.subTest(key=key):
                with closing(sqlite3.connect(self.hub.index_path)) as connection:
                    connection.execute("UPDATE meta SET value=? WHERE key=?", (value, key))
                    connection.commit()

                initialized = self.hub.initialize()

                self.assertTrue(initialized["ok"], initialized)
                self.assertTrue(self.hub.doctor()["ok"])
                found = self.hub.search("自动恢复样本")
                self.assertEqual(found["items"][0]["ref"], f"event:{event['event_id']}")

    def test_doctor_reports_malformed_manifest_shape(self) -> None:
        self.hub.manifest_path.write_text("[]\n", encoding="utf-8", newline="\n")

        diagnostic = self.hub.doctor()

        self.assertFalse(diagnostic["ok"])
        self.assertTrue(
            any("malformed manifest.json" in error for error in diagnostic["errors"]),
            diagnostic,
        )

    def test_doctor_reports_malformed_event_field_types(self) -> None:
        self.hub.put(
            action="append",
            kind="fact",
            content="事件字段类型损坏样本",
            source_type="test",
            source_ref="synthetic:malformed-event",
            confirmed=True,
        )
        original = read_jsonl(self.hub.events_path)[0]
        cases = {
            "action": [],
            "kind": {},
            "project_id": [],
            "created_at": "not-a-timestamp",
        }

        for field, value in cases.items():
            with self.subTest(field=field):
                damaged = dict(original)
                damaged[field] = value
                self.hub.events_path.write_text(
                    json.dumps(damaged, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                    newline="\n",
                )
                diagnostic = self.hub.doctor()
                self.assertFalse(diagnostic["ok"])
                self.assertTrue(
                    any("event at line 1" in error for error in diagnostic["errors"]),
                    diagnostic,
                )

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
            self.assertEqual(manifest["scope"], "authoritative-sources-only")
            for entry in manifest["entries"]:
                payload = archive.read(entry["path"])
                self.assertEqual(hashlib.sha256(payload).hexdigest(), entry["sha256"])
                self.assertEqual(len(payload), entry["bytes"])

    def test_backup_excludes_index_backups_and_unapproved_private_files(self) -> None:
        private_sentinel = b"PRIVATE-SENTINEL-MUST-NOT-LEAVE-DATA-ROOT"
        (self.hub.root / "private-notes.txt").write_bytes(private_sentinel)
        (self.hub.backups_dir / "older.zip").write_bytes(private_sentinel)
        result = self.hub.backup(self.base / "exports")

        with zipfile.ZipFile(result["backup"]) as archive:
            self.assertNotIn("index.sqlite3", archive.namelist())
            self.assertNotIn("private-notes.txt", archive.namelist())
            self.assertNotIn("backups/older.zip", archive.namelist())
            for name in archive.namelist():
                self.assertNotIn(private_sentinel, archive.read(name))

    def test_verified_restore_rebuilds_index_and_preserves_stable_refs(self) -> None:
        event = self.hub.put(
            action="append",
            kind="fact",
            content="恢复后可以检索的纯合成事件",
            source_type="test",
            source_ref="synthetic:restore",
            confirmed=True,
        )
        backup = self.hub.backup(self.base / "exports")
        destination = self.base / "restored"

        result = ContextHub.restore(backup["backup"], destination)

        restored = ContextHub(destination)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["reindexed"])
        self.assertTrue(restored.index_path.is_file())
        self.assertTrue(restored.doctor()["ok"])
        found = restored.search("恢复后可以检索")
        self.assertEqual(found["items"][0]["ref"], f"event:{event['event_id']}")
        self.assertEqual(
            [entry["path"] for entry in result["entries"]],
            ["config.toml", "manifest.json", "memory/events.jsonl"],
        )

    def test_restore_rejects_tampering_without_creating_destination(self) -> None:
        self.hub.put(
            action="append",
            kind="fact",
            content="备份篡改拒绝样本",
            source_type="test",
            source_ref="synthetic:restore-tamper",
            confirmed=True,
        )
        original = Path(self.hub.backup(self.base / "exports")["backup"])
        corrupted = self.base / "corrupted.zip"
        with zipfile.ZipFile(original) as source, zipfile.ZipFile(
            corrupted, "w", compression=zipfile.ZIP_DEFLATED
        ) as target:
            for info in source.infolist():
                payload = source.read(info.filename)
                if info.filename == "memory/events.jsonl":
                    payload += b"tampered"
                target.writestr(info.filename, payload)

        destination = self.base / "must-not-exist"
        with self.assertRaisesRegex(ValidationError, "byte count mismatch"):
            ContextHub.restore(corrupted, destination)
        self.assertFalse(destination.exists())

    def test_restore_rejects_unexpected_archive_members(self) -> None:
        original = Path(self.hub.backup(self.base / "exports")["backup"])
        unexpected = self.base / "unexpected.zip"
        with zipfile.ZipFile(original) as source, zipfile.ZipFile(
            unexpected, "w", compression=zipfile.ZIP_DEFLATED
        ) as target:
            for info in source.infolist():
                target.writestr(info.filename, source.read(info.filename))
            target.writestr("private/extra.txt", "must be rejected")

        destination = self.base / "unexpected-restore"
        with self.assertRaisesRegex(ValidationError, "missing or unexpected"):
            ContextHub.restore(unexpected, destination)
        self.assertFalse(destination.exists())

    def test_restore_rejects_encrypted_archive_members(self) -> None:
        original = Path(self.hub.backup(self.base / "exports")["backup"])
        encrypted = self.base / "encrypted-flag.zip"
        payload = bytearray(original.read_bytes())
        for signature, flag_offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
            cursor = 0
            while (header := payload.find(signature, cursor)) >= 0:
                flags = int.from_bytes(payload[header + flag_offset : header + flag_offset + 2], "little")
                payload[header + flag_offset : header + flag_offset + 2] = (flags | 0x1).to_bytes(
                    2, "little"
                )
                cursor = header + len(signature)
        encrypted.write_bytes(payload)

        destination = self.base / "encrypted-restore"
        with self.assertRaisesRegex(ValidationError, "must not be encrypted"):
            ContextHub.restore(encrypted, destination)
        self.assertFalse(destination.exists())

    def test_restore_rejects_non_regular_archive_members(self) -> None:
        original = Path(self.hub.backup(self.base / "exports")["backup"])
        non_regular = self.base / "non-regular.zip"
        with zipfile.ZipFile(original) as source, zipfile.ZipFile(
            non_regular, "w", compression=zipfile.ZIP_DEFLATED
        ) as target:
            for info in source.infolist():
                payload = source.read(info.filename)
                if info.filename == "memory/events.jsonl":
                    symlink = zipfile.ZipInfo(info.filename, info.date_time)
                    symlink.create_system = 3
                    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
                    symlink.compress_type = zipfile.ZIP_DEFLATED
                    target.writestr(symlink, payload)
                else:
                    target.writestr(info, payload)

        destination = self.base / "non-regular-restore"
        with self.assertRaisesRegex(ValidationError, "must be regular files"):
            ContextHub.restore(non_regular, destination)
        self.assertFalse(destination.exists())

    def test_restore_rejects_wrong_backup_scope(self) -> None:
        original = Path(self.hub.backup(self.base / "exports")["backup"])
        wrong_scope = self.base / "wrong-scope.zip"
        with zipfile.ZipFile(original) as source, zipfile.ZipFile(
            wrong_scope, "w", compression=zipfile.ZIP_DEFLATED
        ) as target:
            for info in source.infolist():
                payload = source.read(info.filename)
                if info.filename == "backup-manifest.json":
                    manifest = json.loads(payload)
                    manifest["scope"] = "whole-data-root"
                    payload = (
                        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
                    ).encode("utf-8")
                target.writestr(info, payload)

        destination = self.base / "wrong-scope-restore"
        with self.assertRaisesRegex(ValidationError, "backup scope"):
            ContextHub.restore(wrong_scope, destination)
        self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()

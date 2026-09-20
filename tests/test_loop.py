from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from context_hub import ContextHub
from context_hub.errors import ValidationError
from context_hub.loop import LoopCoordinator

from tests.helpers import write_markdown_project


class LoopCoordinatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="context-hub-loop-")
        self.base = Path(self.temporary.name)
        self.hub = ContextHub(self.base / "hub")
        self.hub.initialize()
        self.loop = LoopCoordinator(self.hub)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_loop_applies_one_safe_action_and_reaches_ready(self) -> None:
        initial = self.loop.quick_status()
        self.assertTrue(initial["index_ok"])
        self.assertEqual(initial["freshness"], "stale")

        synchronized = self.loop.loop_check(apply_safe=True)
        self.assertEqual(synchronized["action"], "sync")
        self.assertTrue(synchronized["applied"])
        self.assertEqual(synchronized["next_action"], "backup")

        backed_up = self.loop.loop_check(apply_safe=True)
        self.assertEqual(backed_up["action"], "backup")
        self.assertTrue(backed_up["evidence"]["ok"])
        self.assertEqual(backed_up["next_action"], "ready")
        self.assertTrue(backed_up["ready"])

        stable = self.loop.loop_check(apply_safe=True)
        self.assertEqual(stable["action"], "ready")
        self.assertFalse(stable["applied"])

        self.hub.put(
            action="append",
            kind="fact",
            content="合成的新鲜度变更",
            source_type="test",
            source_ref="synthetic:freshness",
            confirmed=True,
        )
        self.assertEqual(self.loop.quick_status()["freshness"], "stale")
        refreshed = self.loop.sync()
        self.assertEqual(refreshed["action"], "synchronized")
        self.assertEqual(self.loop.sync()["action"], "throttled")

    def test_import_approval_recomputes_snapshot_without_persisting_private_paths(self) -> None:
        source_root = self.base / "candidate"
        source = write_markdown_project(
            source_root,
            title="真实导入门禁测试",
            body="这只是隔离目录中的合成占位内容。",
        )
        plan = self.loop.import_plan(
            project_id="future-real",
            root=source_root,
            files=["AGENTS.md"],
            classification="real",
        )
        self.assertTrue(plan["ok"])
        self.assertTrue(plan["dry_run"])
        self.assertFalse(plan["mutated"])
        self.assertFalse(plan["approved"])
        self.assertFalse(self.loop.state_path.exists())
        self.assertNotIn(str(source_root.resolve()), json.dumps(plan, ensure_ascii=False))
        self.assertEqual(
            set(plan["candidates"][0]),
            {"relative_path", "bytes", "mtime_ns", "ctime_ns", "sha256"},
        )

        approved = self.loop.approve_import(
            plan["plan_hash"],
            project_id="future-real",
            root=source_root,
            files=["AGENTS.md"],
            classification="real",
            confirmed=True,
        )
        self.assertTrue(approved["approved"])
        self.assertEqual(approved["candidate_count"], 1)
        state_text = self.loop.state_path.read_text(encoding="utf-8")
        self.assertNotIn(str(source_root.resolve()), state_text)
        self.assertNotIn("合成占位内容", state_text)

        identical_root = self.base / "identical-candidate"
        identical_root.mkdir()
        (identical_root / "AGENTS.md").write_bytes(source.read_bytes())
        identical_plan = self.loop.import_plan(
            project_id="future-real",
            root=identical_root,
            files=["AGENTS.md"],
            classification="real",
        )
        self.assertNotEqual(identical_plan["plan_hash"], plan["plan_hash"])
        self.assertNotIn(str(identical_root.resolve()), json.dumps(identical_plan, ensure_ascii=False))
        with self.assertRaisesRegex(ValidationError, "plan changed"):
            self.loop.approve_import(
                plan["plan_hash"],
                project_id="future-real",
                root=identical_root,
                files=["AGENTS.md"],
                classification="real",
                confirmed=True,
            )

        original_stat = source.stat()
        os.utime(
            source,
            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns + 2_000_000_000),
        )
        with self.assertRaisesRegex(ValidationError, "plan changed"):
            self.loop.approve_import(
                plan["plan_hash"],
                project_id="future-real",
                root=source_root,
                files=["AGENTS.md"],
                classification="real",
                confirmed=True,
            )
        metadata_changed = self.loop.import_plan(
            project_id="future-real",
            root=source_root,
            files=["AGENTS.md"],
            classification="real",
        )
        self.assertNotEqual(metadata_changed["plan_hash"], plan["plan_hash"])

        source.write_text("# 已改变\n\n合成快照已经变化。\n", encoding="utf-8", newline="\n")
        with self.assertRaisesRegex(ValidationError, "plan changed"):
            self.loop.approve_import(
                metadata_changed["plan_hash"],
                project_id="future-real",
                root=source_root,
                files=["AGENTS.md"],
                classification="real",
                confirmed=True,
            )
        changed = self.loop.import_plan(
            project_id="future-real",
            root=source_root,
            files=["AGENTS.md"],
            classification="real",
        )
        self.assertNotEqual(changed["plan_hash"], metadata_changed["plan_hash"])
        self.assertFalse(changed["approved"])

        rejected = self.loop.import_plan(
            project_id="future-real",
            root=source_root,
            files=["../private.txt"],
            classification="real",
        )
        self.assertFalse(rejected["ok"])
        self.assertEqual(rejected["rejected"], [{"entry": "[invalid-entry]", "reason": "ValidationError"}])
        self.assertNotIn("private.txt", json.dumps(rejected))

    def test_worker_limits_capture_overshoot_and_truncation_without_task_text(self) -> None:
        exact = self.loop.worker_policy(max_total_tokens=1_000_000)
        self.assertTrue(exact["accepted"])
        self.assertFalse(self.loop.worker_policy(max_total_tokens=1_000_001)["accepted"])

        recorded = self.loop.record_worker(
            task_id="synthetic-worker-001",
            max_total_tokens=1_000_000,
            observed_tokens=1_006_464,
            attempts=1,
            output_chars=70_000,
            wall_seconds=12.5,
            outcome="token_budget",
        )
        worker = recorded["worker"]
        self.assertTrue(worker["token_overshoot"])
        self.assertTrue(worker["output_truncated"])
        self.assertTrue(worker["protection_exceeded"])
        self.assertTrue(worker["needs_review"])
        self.assertEqual(worker["output_chars"], 65_536)
        self.assertNotIn("synthetic-worker-001", self.loop.state_path.read_text(encoding="utf-8"))
        self.loop.sync(force=True)
        self.assertEqual(self.loop.loop_check()["action"], "inspect_worker")

        wall_recorded = self.loop.record_worker(
            task_id="synthetic-worker-wall-001",
            max_total_tokens=1_000_000,
            observed_tokens=100,
            attempts=1,
            output_chars=100,
            wall_seconds=3_600.001,
            outcome="completed",
        )
        self.assertTrue(wall_recorded["worker"]["wall_time_exceeded"])
        self.assertTrue(wall_recorded["worker"]["protection_exceeded"])
        self.assertTrue(wall_recorded["worker"]["needs_review"])
        self.assertEqual(self.loop.loop_check()["action"], "inspect_worker")

        with self.assertRaises(ValidationError):
            self.loop.record_worker(
                task_id="synthetic-worker-002",
                max_total_tokens=1_000_000,
                observed_tokens=1,
                attempts=4,
                output_chars=1,
                wall_seconds=1,
                outcome="failed",
            )

    def test_synthetic_evaluation_and_opt_in_telemetry_are_bounded(self) -> None:
        first = self.base / "project-a"
        second = self.base / "project-b"
        write_markdown_project(first, title="Alpha", body="独有检索词 alpha-loop-needle")
        write_markdown_project(second, title="Beta", body="独有检索词 beta-loop-needle")
        self.hub.register_project("alpha", first)
        self.hub.register_project("beta", second)
        cases = self.base / "cases.json"
        cases.write_text(
            json.dumps(
                {
                    "synthetic_only": True,
                    "cases": [
                        {"id": "alpha-top1", "query": "alpha-loop-needle", "expected_project_id": "alpha"},
                        {"id": "beta-top1", "query": "beta-loop-needle", "expected_project_id": "beta"},
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
            newline="\n",
        )
        result = self.loop.evaluate(cases)
        self.assertTrue(result["ok"])
        self.assertEqual(result["top1_accuracy"], 1.0)
        self.assertEqual(result["top3_accuracy"], 1.0)

        config = self.hub.config_path.read_text(encoding="utf-8")
        self.hub.config_path.write_text(
            config.replace(
                "[telemetry]\nenabled = false",
                "[telemetry]\nenabled = \"false\"",
                1,
            ),
            encoding="utf-8",
            newline="\n",
        )
        with self.assertRaisesRegex(ValidationError, "telemetry.enabled must be a boolean"):
            self.loop.quick_status()
        config = self.hub.config_path.read_text(encoding="utf-8")
        self.hub.config_path.write_text(
            config.replace(
                "[telemetry]\nenabled = \"false\"",
                "[telemetry]\nenabled = true",
                1,
            ),
            encoding="utf-8",
            newline="\n",
        )
        self.loop.sync(force=True)
        self.loop.quick_status()
        events = [
            json.loads(line)
            for line in self.loop.telemetry_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertGreaterEqual(len(events), 2)
        allowed = {"schema_version", "at", "operation", "outcome", "classification", "duration_ms", "counts"}
        self.assertTrue(all(set(event) <= allowed for event in events))
        serialized = json.dumps(events, ensure_ascii=False)
        self.assertNotIn("alpha-loop-needle", serialized)
        self.assertNotIn(str(first.resolve()), serialized)


if __name__ == "__main__":
    unittest.main()

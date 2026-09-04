from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from context_hub import ContextHub
from context_hub.errors import NotFoundError, ValidationError

from tests.helpers import read_jsonl, write_markdown_project


class CoreContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="context-hub-core-")
        self.base = Path(self.temporary.name)
        self.data_dir = self.base / "data"
        self.hub = ContextHub(self.data_dir)
        self.hub.initialize()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_unicode_hash_and_cursor_pagination_round_trip(self) -> None:
        content = "中文🙂\\路径\n\"引号\"\r\n\n" + "页码内容" * 180
        result = self.hub.put(
            action="append",
            kind="fact",
            content=content,
            source_type="test",
            source_ref="synthetic:unicode",
            confirmed=True,
        )
        expected_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        self.assertEqual(result["content_sha256"], expected_hash)

        ref = f"event:{result['event_id']}"
        chunks: list[str] = []
        cursor: str | None = None
        expected_offset = 0
        while True:
            page = self.hub.read(ref if cursor is None else None, cursor=cursor, max_chars=200)
            self.assertEqual(page["ref"], ref)
            self.assertEqual(page["char_offset"], expected_offset)
            self.assertEqual(page["sha256"], expected_hash)
            chunks.append(page["content"])
            expected_offset += len(page["content"])
            cursor = page["next_cursor"]
            if cursor is None:
                self.assertFalse(page["truncated"])
                break
        self.assertEqual("".join(chunks), content)
        self.assertEqual(expected_offset, len(content))

        stored = read_jsonl(self.hub.events_path)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["content"], content)
        self.assertEqual(stored[0]["content_sha256"], expected_hash)

    def test_search_uses_trigram_and_bounded_like(self) -> None:
        long_event = self.hub.put(
            action="append",
            kind="decision",
            content="采用耐久性验证作为合成测试关键词",
            source_type="test",
            source_ref="synthetic:search",
            confirmed=True,
        )

        fts = self.hub.search("耐久性")
        self.assertEqual(fts["mode"], "fts5-trigram")
        self.assertEqual(fts["items"][0]["ref"], f"event:{long_event['event_id']}")
        short = self.hub.search("耐")
        self.assertEqual(short["mode"], "bounded-like")
        self.assertEqual(short["items"][0]["ref"], f"event:{long_event['event_id']}")

        percent = self.hub.put(
            action="append",
            kind="fact",
            content="literal % marker and literal _ marker",
            source_type="test",
            source_ref="synthetic:wildcards",
            confirmed=True,
        )
        self.assertEqual(self.hub.search("%")["items"][0]["ref"], f"event:{percent['event_id']}")
        self.assertEqual(self.hub.search("_")["items"][0]["ref"], f"event:{percent['event_id']}")

    def test_project_filter_kind_filter_and_source_location(self) -> None:
        project_a = self.base / "project-a"
        project_b = self.base / "project-b"
        write_markdown_project(project_a, title="架构", body="共享线索 alpha-only")
        write_markdown_project(project_b, title="状态", body="共享线索 beta-only")
        self.hub.register_project("alpha", project_a)
        self.hub.register_project("beta", project_b)
        event = self.hub.put(
            action="append",
            kind="constraint",
            project_id="alpha",
            content="共享线索 event-only",
            source_type="test",
            source_ref="synthetic:project",
            confirmed=True,
        )

        alpha = self.hub.search("共享线索", project_id="alpha", limit=5)
        self.assertTrue(alpha["items"])
        self.assertTrue(all(item["project_id"] == "alpha" for item in alpha["items"]))
        event_only = self.hub.search("共享线索", project_id="alpha", kinds=["constraint"], limit=5)
        self.assertEqual([item["ref"] for item in event_only["items"]], [f"event:{event['event_id']}"])

        file_item = next(item for item in alpha["items"] if item["kind"] == "markdown")
        full = self.hub.read(file_item["ref"], max_chars=4000)
        self.assertEqual(full["source"]["ref"], "alpha:AGENTS.md")
        self.assertEqual(full["location"]["path"], "AGENTS.md")
        self.assertEqual(full["location"]["heading"], "架构")
        self.assertEqual(full["location"]["line_start"], 1)

    def test_default_registration_discovers_direct_context_markdown_only(self) -> None:
        project = self.base / "project-context-glob"
        write_markdown_project(project, title="入口", body="合成入口文档")
        context_dir = project / "context"
        nested_dir = context_dir / "nested"
        nested_dir.mkdir(parents=True)
        (context_dir / "notes.md").write_text(
            "# Notes\n\n直接上下文文档可检索。\n", encoding="utf-8", newline="\n"
        )
        (context_dir / "ignored.txt").write_text(
            "非 Markdown 不应自动注册。\n", encoding="utf-8", newline="\n"
        )
        (nested_dir / "nested.md").write_text(
            "嵌套 Markdown 不应自动注册。\n", encoding="utf-8", newline="\n"
        )

        registered = self.hub.register_project("context-glob", project)

        self.assertEqual(registered["files"], ["AGENTS.md", "context/notes.md"])
        direct = self.hub.search("直接上下文", project_id="context-glob")
        self.assertEqual(direct["items"][0]["source"]["ref"], "context-glob:context/notes.md")
        self.assertEqual(self.hub.search("嵌套 Markdown", project_id="context-glob")["items"], [])

    def test_supersede_tombstone_and_history_visibility(self) -> None:
        old = self.hub.put(
            action="append",
            kind="preference",
            content="旧版偏好仅用于合成历史",
            source_type="test",
            source_ref="synthetic:old",
            confirmed=True,
        )
        new = self.hub.put(
            action="supersede",
            kind="preference",
            content="新版偏好仅用于合成历史",
            source_type="test",
            source_ref="synthetic:new",
            supersedes=old["event_id"],
            confirmed=True,
        )
        self.assertEqual(self.hub.search("旧版偏好")["items"], [])
        old_history = self.hub.search("旧版偏好", include_history=True)
        self.assertTrue(old_history["items"][0]["superseded"])
        self.assertTrue(self.hub.read(f"event:{old['event_id']}")["superseded"])

        self.hub.put(
            action="tombstone",
            kind="preference",
            content="",
            source_type="test",
            source_ref="synthetic:tombstone",
            supersedes=new["event_id"],
            confirmed=True,
        )
        self.assertEqual(self.hub.search("新版偏好")["items"], [])
        self.assertEqual(
            self.hub.search("新版偏好", include_history=True)["items"][0]["ref"],
            f"event:{new['event_id']}",
        )

    def test_reindex_preserves_refs_until_source_changes(self) -> None:
        project = self.base / "project"
        source = write_markdown_project(project, title="决定", body="稳定引用验证文本")
        self.hub.register_project("stable", project)
        first = self.hub.search("稳定引用")["items"][0]
        self.hub.reindex()
        second = self.hub.search("稳定引用")["items"][0]
        self.assertEqual(first["ref"], second["ref"])
        self.assertEqual(first["source"], second["source"])

        source.write_text("# 决定\n\n稳定引用验证文本，内容已改变。\n", encoding="utf-8", newline="\n")
        self.hub.initialize()
        changed = self.hub.search("稳定引用")["items"][0]
        self.assertNotEqual(changed["ref"], first["ref"])
        with self.assertRaises(NotFoundError):
            self.hub.read(first["ref"])

    def test_allowlist_escape_case_and_symlink_protection(self) -> None:
        project = self.base / "project"
        write_markdown_project(project, title="Allowed", body="合成允许列表")
        outside = self.base / "outside.md"
        outside.write_text("outside", encoding="utf-8")

        with self.assertRaises(ValidationError):
            self.hub.register_project("escape", project, ["../outside.md"])
        with self.assertRaises(ValidationError):
            self.hub.register_project("absolute", project, [str(outside.resolve())])

        deduplicated = self.hub.register_project("casefold", project, ["AGENTS.md", "agents.md"])
        self.assertEqual(deduplicated["files"], ["AGENTS.md"])

        link = project / "linked.md"
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable on this host")
        with self.assertRaises(ValidationError):
            self.hub.register_project("symlink", project, ["linked.md"])


if __name__ == "__main__":
    unittest.main()

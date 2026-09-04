from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from context_hub import ContextHub
from context_hub.mcp_stdio import build_server


class MCPContractTest(unittest.IsolatedAsyncioTestCase):
    def test_library_import_preserves_the_public_mcp_package(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        script = (
            "import sys; "
            "import context_hub.mcp_stdio; "
            "assert 'mcp' not in sys.modules; "
            "from mcp import ClientSession; "
            "print(ClientSession.__name__)"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "ClientSession")

    async def test_init_can_enable_writes_without_later_disabling_them(self) -> None:
        with tempfile.TemporaryDirectory(prefix="context-hub-mcp-write-config-") as temporary:
            hub = ContextHub(temporary)
            self.assertFalse(hub.initialize()["write_enabled"])
            self.assertTrue(hub.initialize(write_enabled=True)["write_enabled"])
            self.assertTrue(hub.initialize()["write_enabled"])

            server = build_server(temporary)
            tools = await server.list_tools()
            self.assertEqual([tool.name for tool in tools], ["context_get", "context_put"])

    async def test_readonly_server_hides_write_and_schema_budget_holds(self) -> None:
        with tempfile.TemporaryDirectory(prefix="context-hub-mcp-schema-") as temporary:
            readonly = build_server(temporary, write_enabled=False)
            tools = await readonly.list_tools()
            self.assertEqual([tool.name for tool in tools], ["context_get"])
            schema_bytes = len(
                json.dumps(
                    [tool.model_dump(mode="json", by_alias=True) for tool in tools],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            self.assertLessEqual(schema_bytes, 3072)
            first_512 = readonly.instructions.encode("utf-8")[:512].decode("utf-8", errors="ignore")
            self.assertIn("context_get", first_512)
            self.assertIn("context_put", first_512)
            self.assertIn("Writes are hidden unless enabled", first_512)

            writable = build_server(temporary, write_enabled=True)
            writable_tools = await writable.list_tools()
            self.assertEqual([tool.name for tool in writable_tools], ["context_get", "context_put"])
            writable_schema_bytes = len(
                json.dumps(
                    [tool.model_dump(mode="json", by_alias=True) for tool in writable_tools],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            self.assertLessEqual(writable_schema_bytes, 3072)

    async def test_default_search_response_stays_under_budget(self) -> None:
        with tempfile.TemporaryDirectory(prefix="context-hub-mcp-response-") as temporary:
            hub = ContextHub(temporary)
            hub.initialize()
            hub.put(
                action="append",
                kind="fact",
                content="预算关键词" + "合成填充" * 900,
                source_type="test",
                source_ref="synthetic:response-budget",
                confirmed=True,
            )
            response = hub.search("预算关键词")
            encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.assertLessEqual(len(encoded), 2560)

    async def test_tool_errors_are_structured_and_do_not_crash(self) -> None:
        with tempfile.TemporaryDirectory(prefix="context-hub-mcp-errors-") as temporary:
            server = build_server(temporary, write_enabled=False)

            unknown = await server.call_tool("not_a_tool", {})
            self.assertTrue(unknown.is_error)
            self.assertIn("Unknown tool", unknown.content[0].text)

            invalid = await server.call_tool("context_get", {"op": "search", "limit": True})
            self.assertTrue(invalid.is_error)
            self.assertIn("limit must be an integer", invalid.content[0].text)

    async def test_real_stdio_session_lists_and_calls_context_get(self) -> None:
        with tempfile.TemporaryDirectory(prefix="context-hub-mcp-stdio-") as temporary:
            hub = ContextHub(temporary)
            hub.initialize()
            hub.put(
                action="append",
                kind="fact",
                content="真实STDIO合成握手样本",
                source_type="test",
                source_ref="synthetic:stdio",
                confirmed=True,
            )
            environment = os.environ.copy()
            environment["CONTEXT_HUB_DATA_DIR"] = temporary
            environment.pop("CONTEXT_HUB_WRITE_ENABLED", None)
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-m", "context_hub.mcp_stdio"],
                env=environment,
                cwd=Path(__file__).resolve().parents[1],
            )
            async with stdio_client(parameters) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await asyncio.wait_for(session.initialize(), timeout=10)
                    tools = await asyncio.wait_for(session.list_tools(), timeout=10)
                    self.assertEqual([tool.name for tool in tools.tools], ["context_get"])
                    result = await asyncio.wait_for(
                        session.call_tool(
                            "context_get",
                            {"op": "search", "query": "STDIO合成", "limit": 3, "max_chars": 600},
                        ),
                        timeout=10,
                    )
                    self.assertFalse(result.is_error)
                    self.assertIsNotNone(result.structured_content)
                    self.assertEqual(result.structured_content["op"], "search")
                    self.assertEqual(len(result.structured_content["items"]), 1)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Verify a Context Hub Streamable HTTP endpoint through the official MCP client."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any, Sequence

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


def _require_marker(payload: dict[str, Any], *, operation: str) -> dict[str, Any]:
    marker = payload.get("context_hub")
    if not isinstance(marker, dict):
        raise AssertionError(f"{operation}: context_hub marker is missing")
    expected = {
        "active": True,
        "status": "invoked",
        "server": "context-hub",
        "transport": "streamable-http",
        "operation": operation,
    }
    for key, value in expected.items():
        if marker.get(key) != value:
            raise AssertionError(
                f"{operation}: context_hub.{key}={marker.get(key)!r}, expected {value!r}"
            )
    request_id = marker.get("request_id")
    if not isinstance(request_id, str) or not request_id.startswith("ch_"):
        raise AssertionError(f"{operation}: invalid request_id")
    return marker


async def verify(
    *,
    url: str,
    query: str,
    project_id: str,
    expected_marker: str,
) -> dict[str, Any]:
    async with streamable_http_client(url) as streams:
        async with ClientSession(*streams) as session:
            initialized = await asyncio.wait_for(session.initialize(), timeout=20)
            listed = await asyncio.wait_for(session.list_tools(), timeout=20)
            tool_names = [tool.name for tool in listed.tools]
            if tool_names != ["context_get"]:
                raise AssertionError(f"read-only endpoint exposed unexpected tools: {tool_names}")

            manifest_result = await asyncio.wait_for(
                session.call_tool("context_get", {"op": "manifest"}),
                timeout=20,
            )
            if manifest_result.is_error or not manifest_result.structured_content:
                raise AssertionError("manifest call failed")
            manifest = manifest_result.structured_content
            manifest_marker = _require_marker(manifest, operation="manifest")
            project_ids = [item.get("project_id") for item in manifest.get("projects", [])]
            if project_id not in project_ids:
                raise AssertionError(f"project {project_id!r} is absent from manifest")

            search_result = await asyncio.wait_for(
                session.call_tool(
                    "context_get",
                    {
                        "op": "search",
                        "query": query,
                        "project_id": project_id,
                        "limit": 3,
                        "max_chars": 600,
                    },
                ),
                timeout=20,
            )
            if search_result.is_error or not search_result.structured_content:
                raise AssertionError("search call failed")
            search = search_result.structured_content
            search_marker = _require_marker(search, operation="search")
            items = search.get("items")
            if not isinstance(items, list) or not items:
                raise AssertionError("search returned no items")
            if expected_marker not in str(items[0].get("snippet", "")):
                raise AssertionError("search result did not contain the expected synthetic marker")
            if search_marker.get("project_ids") != [project_id]:
                raise AssertionError("search source marker did not preserve project scope")
            if search_marker.get("source_types") != ["project_file"]:
                raise AssertionError("search source marker did not identify the registered project file")

            ref = items[0].get("ref")
            if not isinstance(ref, str) or not ref:
                raise AssertionError("search result did not provide a stable ref")
            read_result = await asyncio.wait_for(
                session.call_tool(
                    "context_get",
                    {"op": "read", "ref": ref, "max_chars": 4000},
                ),
                timeout=20,
            )
            if read_result.is_error or not read_result.structured_content:
                raise AssertionError("read call failed")
            read = read_result.structured_content
            read_marker = _require_marker(read, operation="read")
            content = read.get("content")
            digest = read.get("sha256")
            if not isinstance(content, str) or expected_marker not in content:
                raise AssertionError("read content did not contain the expected synthetic marker")
            if digest != hashlib.sha256(content.encode("utf-8")).hexdigest():
                raise AssertionError("read SHA-256 did not match the complete content")
            if read.get("truncated") or read.get("next_cursor") is not None:
                raise AssertionError("verification fixture unexpectedly required pagination")

    request_ids = [
        manifest_marker["request_id"],
        search_marker["request_id"],
        read_marker["request_id"],
    ]
    checks = {
        "initialized": True,
        "read_only_tool_surface": tool_names == ["context_get"],
        "manifest_project_visible": project_id in project_ids,
        "search_marker_exact": search_marker.get("project_ids") == [project_id],
        "read_content_and_hash_exact": True,
        "request_ids_unique": len(set(request_ids)) == 3,
        "streamable_http_transport_reported": all(
            marker.get("transport") == "streamable-http"
            for marker in (manifest_marker, search_marker, read_marker)
        ),
    }
    return {
        "ok": all(checks.values()),
        "synthetic_only": True,
        "endpoint": url,
        "protocol_version": initialized.protocol_version,
        "tools": tool_names,
        "query": query,
        "project_id": project_id,
        "stable_ref": ref,
        "content_sha256": digest,
        "invocations": {
            "manifest": manifest_marker,
            "search": search_marker,
            "read": read_marker,
        },
        "checks": checks,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--expected-marker", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    report = asyncio.run(
        verify(
            url=args.url,
            query=args.query,
            project_id=args.project_id,
            expected_marker=args.expected_marker,
        )
    )
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8", newline="\n")
    print(encoded, end="")
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

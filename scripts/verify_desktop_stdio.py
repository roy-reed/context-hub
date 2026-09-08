#!/usr/bin/env python3
"""Verify the ChatGPT Desktop Context Hub STDIO command with synthetic data."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import statistics
import time
from pathlib import Path
from typing import Any, Sequence

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _require_marker(payload: dict[str, Any], *, operation: str) -> dict[str, Any]:
    marker = payload.get("context_hub")
    if not isinstance(marker, dict):
        raise AssertionError(f"{operation}: context_hub marker is missing")
    expected = {
        "active": True,
        "status": "invoked",
        "server": "context-hub",
        "transport": "stdio",
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


async def _verify_once(
    *,
    command: Path,
    data_dir: Path,
    query: str,
    project_id: str,
    expected_marker: str,
    timeout: float,
) -> dict[str, Any]:
    environment = os.environ.copy()
    environment["CONTEXT_HUB_DATA_DIR"] = str(data_dir)
    environment["CONTEXT_HUB_WRITE_ENABLED"] = "0"
    parameters = StdioServerParameters(command=str(command), args=[], env=environment)
    started = time.perf_counter()

    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            initialized = await asyncio.wait_for(session.initialize(), timeout=timeout)
            listed = await asyncio.wait_for(session.list_tools(), timeout=timeout)
            tool_names = [tool.name for tool in listed.tools]
            if tool_names != ["context_get"]:
                raise AssertionError(f"read-only endpoint exposed unexpected tools: {tool_names}")
            output_schema = listed.tools[0].output_schema
            if not isinstance(output_schema, dict):
                raise AssertionError("context_get did not declare outputSchema")
            required = output_schema.get("required")
            properties = output_schema.get("properties")
            if not isinstance(required, list) or not {"op", "context_hub"}.issubset(required):
                raise AssertionError("context_get outputSchema has an incomplete required contract")
            if not isinstance(properties, dict) or not {
                "items",
                "content",
                "projects",
                "context_hub",
            }.issubset(properties):
                raise AssertionError("context_get outputSchema has incomplete result properties")

            manifest_result = await asyncio.wait_for(
                session.call_tool("context_get", {"op": "manifest"}), timeout=timeout
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
                timeout=timeout,
            )
            if search_result.is_error or not search_result.structured_content:
                raise AssertionError("search call failed")
            search = search_result.structured_content
            search_marker = _require_marker(search, operation="search")
            items = search.get("items")
            if not isinstance(items, list) or not items:
                raise AssertionError("search returned no items")
            if expected_marker not in str(items[0].get("snippet", "")):
                raise AssertionError("search did not return the expected synthetic marker")
            if search_marker.get("project_ids") != [project_id]:
                raise AssertionError("search source marker did not preserve project scope")

            ref = items[0].get("ref")
            if not isinstance(ref, str) or not ref:
                raise AssertionError("search result did not provide a stable ref")
            read_result = await asyncio.wait_for(
                session.call_tool(
                    "context_get", {"op": "read", "ref": ref, "max_chars": 4000}
                ),
                timeout=timeout,
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
    return {
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "protocol_version": initialized.protocol_version,
        "tools": tool_names,
        "stable_ref": ref,
        "content_sha256": digest,
        "request_ids": request_ids,
        "request_ids_unique": len(set(request_ids)) == 3,
        "markers": {
            "manifest": manifest_marker,
            "search": search_marker,
            "read": read_marker,
        },
    }


async def verify(
    *,
    command: Path,
    data_dir: Path,
    query: str,
    project_id: str,
    expected_marker: str,
    runs: int,
    timeout: float,
) -> dict[str, Any]:
    if not command.is_file():
        raise FileNotFoundError(f"STDIO command not found: {command}")
    if not data_dir.is_dir():
        raise FileNotFoundError(f"synthetic data directory not found: {data_dir}")
    if runs < 1:
        raise ValueError("runs must be at least 1")

    samples = []
    for _ in range(runs):
        samples.append(
            await _verify_once(
                command=command,
                data_dir=data_dir,
                query=query,
                project_id=project_id,
                expected_marker=expected_marker,
                timeout=timeout,
            )
        )

    refs = {sample["stable_ref"] for sample in samples}
    digests = {sample["content_sha256"] for sample in samples}
    protocol_versions = {sample["protocol_version"] for sample in samples}
    checks = {
        "fresh_process_runs_completed": len(samples) == runs,
        "read_only_tool_surface": all(sample["tools"] == ["context_get"] for sample in samples),
        "context_get_output_schema_declared": True,
        "manifest_search_read_marked": all(
            all(marker["active"] for marker in sample["markers"].values()) for sample in samples
        ),
        "stable_ref_across_processes": len(refs) == 1,
        "content_hash_across_processes": len(digests) == 1,
        "request_ids_unique_per_run": all(sample["request_ids_unique"] for sample in samples),
    }
    elapsed = [sample["elapsed_ms"] for sample in samples]
    return {
        "ok": all(checks.values()),
        "synthetic_only": True,
        "transport": "stdio",
        "runs": runs,
        "protocol_versions": sorted(protocol_versions),
        "query": query,
        "project_id": project_id,
        "stable_ref": next(iter(refs)) if len(refs) == 1 else None,
        "content_sha256": next(iter(digests)) if len(digests) == 1 else None,
        "timings": {
            "min_ms": min(elapsed),
            "median_ms": round(statistics.median(elapsed), 3),
            "max_ms": max(elapsed),
            "samples_ms": elapsed,
        },
        "checks": checks,
        "invocations": [sample["markers"] for sample in samples],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--command", required=True, type=Path)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--query", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--expected-marker", required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    report = asyncio.run(
        verify(
            command=args.command.resolve(),
            data_dir=args.data_dir.resolve(),
            query=args.query,
            project_id=args.project_id,
            expected_marker=args.expected_marker,
            runs=args.runs,
            timeout=args.timeout,
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

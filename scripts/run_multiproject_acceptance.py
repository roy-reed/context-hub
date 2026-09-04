"""Run the synthetic multi-project Context Hub acceptance suite.

The suite deliberately creates every fact source below one temporary root. It
does not inspect the caller's home directory, an existing Context Hub data
directory, or any real project files.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Sequence

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from context_hub import ContextHub
from context_hub.errors import NotFoundError
from context_hub.hub import sha256_text


PROJECT_SPECS: tuple[dict[str, str], ...] = (
    {
        "project_id": "aurora",
        "stable_marker": "极光隔离标记",
        "english_marker": "auroraquartz",
        "short_marker": "兮",
        "page_marker": "极光分页证据",
        "event_marker": "极光事件决策标记",
    },
    {
        "project_id": "bamboo",
        "stable_marker": "竹海隔离标记",
        "english_marker": "bamboocircuit",
        "short_marker": "甯",
        "page_marker": "竹海分页证据",
        "event_marker": "竹海事件决策标记",
    },
    {
        "project_id": "cobalt",
        "stable_marker": "钴蓝隔离标记",
        "english_marker": "cobaltvector",
        "short_marker": "彧",
        "page_marker": "钴蓝分页证据",
        "event_marker": "钴蓝事件决策标记",
    },
)

EVENT_IDS = {
    "aurora": "evt_00000000-0000-4000-8000-000000000001",
    "bamboo": "evt_00000000-0000-4000-8000-000000000002",
    "cobalt": "evt_00000000-0000-4000-8000-000000000003",
    "history_old": "evt_00000000-0000-4000-8000-000000000004",
    "history_new": "evt_00000000-0000-4000-8000-000000000005",
}

EDIT_OLD_MARKER = "极光旧版策略待替换"
EDIT_NEW_MARKER = "极光新版策略已经启用并通过合成检查"
HISTORY_OLD_MARKER = "钴蓝旧版合成发布窗格"
HISTORY_NEW_MARKER = "钴蓝新版合成发布窗口"


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _fixture_documents(spec: dict[str, str]) -> dict[str, str]:
    project_id = spec["project_id"]
    pagination_body = " ".join(
        f"{spec['page_marker']}第{index:02d}段只用于验证游标、顺序与哈希。"
        for index in range(1, 49)
    )
    editable = EDIT_OLD_MARKER if project_id == "aurora" else f"{project_id}固定合成策略"
    agents = (
        "# 共享架构决定\n\n"
        f"这是 {project_id} 的纯合成文档。唯一隔离词为 {spec['stable_marker']}。"
        "共享评审线索只用于验证同名章节的项目过滤。\n\n"
        "## 可变发布策略\n\n"
        f"当前策略标记为 {editable}。这里没有真实人员、机构、项目或记忆内容。\n\n"
        "## 分页证据\n\n"
        f"{pagination_body}\n"
    )
    notes = (
        "# 共享运行状态\n\n"
        f"{project_id} 的英文唯一标记是 {spec['english_marker']}，"
        f"单字回退标记是 {spec['short_marker']}。共享评审线索仍然是纯合成文本。\n"
    )
    return {"AGENTS.md": agents, "context/notes.md": notes}


def _write_fixture(root: Path, documents: dict[str, str]) -> None:
    for relative_path, content in documents.items():
        destination = root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8", newline="\n")


def _read_all(hub: ContextHub, ref: str, *, max_chars: int = 200) -> dict[str, Any]:
    pieces: list[str] = []
    cursor: str | None = None
    first: dict[str, Any] | None = None
    expected_offset = 0
    pages = 0
    while True:
        page = hub.read(ref if cursor is None else None, cursor=cursor, max_chars=max_chars)
        if first is None:
            first = page
        if page["char_offset"] != expected_offset:
            raise RuntimeError(f"non-contiguous read cursor for {ref}")
        pieces.append(page["content"])
        expected_offset += len(page["content"])
        pages += 1
        if not page["truncated"]:
            break
        cursor = page["next_cursor"]
        if not isinstance(cursor, str) or pages > 100:
            raise RuntimeError(f"invalid or unbounded read cursor for {ref}")

    if first is None:
        raise RuntimeError(f"read returned no pages for {ref}")
    return {
        "ref": ref,
        "content": "".join(pieces),
        "sha256": first["sha256"],
        "total_chars": first["total_chars"],
        "pages": pages,
        "kind": first["kind"],
        "project_id": first["project_id"],
        "source": first["source"],
        "location": first.get("location"),
    }


def _ref_is_missing(hub: ContextHub, ref: str) -> bool:
    try:
        hub.read(ref)
    except NotFoundError:
        return True
    return False


def _find_item(
    hub: ContextHub,
    query: str,
    *,
    project_id: str,
    source_ref: str | None = None,
    expected_ref: str | None = None,
    kinds: list[str] | None = None,
) -> dict[str, Any]:
    result = hub.search(
        query,
        project_id=project_id,
        kinds=kinds,
        limit=5,
        max_chars=1_200,
    )
    for item in result["items"]:
        if expected_ref is not None and item["ref"] != expected_ref:
            continue
        if source_ref is not None and item["source"]["ref"] != source_ref:
            continue
        return item
    raise RuntimeError(f"expected synthetic result missing for {project_id}:{query}")


def _evaluate_queries(
    hub: ContextHub,
    event_refs: dict[str, str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    cases: list[dict[str, Any]] = []
    for spec in PROJECT_SPECS:
        project_id = spec["project_id"]
        cases.extend(
            [
                {
                    "name": f"{project_id}-chinese-file",
                    "query": spec["stable_marker"],
                    "project_id": project_id,
                    "source_ref": f"{project_id}:AGENTS.md",
                    "mode": "fts5-trigram",
                    "kinds": None,
                },
                {
                    "name": f"{project_id}-english-file",
                    "query": spec["english_marker"],
                    "project_id": project_id,
                    "source_ref": f"{project_id}:context/notes.md",
                    "mode": "fts5-trigram",
                    "kinds": None,
                },
                {
                    "name": f"{project_id}-short-file",
                    "query": spec["short_marker"],
                    "project_id": project_id,
                    "source_ref": f"{project_id}:context/notes.md",
                    "mode": "bounded-like",
                    "kinds": None,
                },
                {
                    "name": f"{project_id}-event-kind",
                    "query": spec["event_marker"],
                    "project_id": project_id,
                    "expected_ref": event_refs[project_id],
                    "mode": "fts5-trigram",
                    "kinds": ["decision"],
                },
            ]
        )

    outcomes: list[dict[str, Any]] = []
    for case in cases:
        result = hub.search(
            case["query"],
            project_id=case["project_id"],
            kinds=case["kinds"],
            limit=3,
            max_chars=1_200,
        )
        expected_positions = [
            index
            for index, item in enumerate(result["items"], start=1)
            if (
                (case.get("expected_ref") is None or item["ref"] == case["expected_ref"])
                and (
                    case.get("source_ref") is None
                    or item["source"]["ref"] == case["source_ref"]
                )
            )
        ]
        outcomes.append(
            {
                "name": case["name"],
                "mode": result["mode"],
                "mode_expected": result["mode"] == case["mode"],
                "top1": expected_positions[:1] == [1],
                "top3": bool(expected_positions),
                "project_isolated": bool(result["items"])
                and all(item["project_id"] == case["project_id"] for item in result["items"]),
                "kind_isolated": case["kinds"] is None
                or all(item["kind"] in case["kinds"] for item in result["items"]),
            }
        )

    case_count = len(outcomes)
    metrics = {
        "cases": case_count,
        "top1_hits": sum(outcome["top1"] for outcome in outcomes),
        "top3_hits": sum(outcome["top3"] for outcome in outcomes),
        "top1_recall": round(sum(outcome["top1"] for outcome in outcomes) / case_count, 4),
        "top3_recall": round(sum(outcome["top3"] for outcome in outcomes) / case_count, 4),
    }
    return metrics, outcomes


def _snapshot(hub: ContextHub, probes: Sequence[dict[str, Any]]) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    for probe in probes:
        item = _find_item(
            hub,
            probe["query"],
            project_id=probe["project_id"],
            source_ref=probe.get("source_ref"),
            expected_ref=probe.get("expected_ref"),
            kinds=probe.get("kinds"),
        )
        read = _read_all(hub, item["ref"], max_chars=300)
        snapshot[probe["name"]] = {
            "ref": read["ref"],
            "sha256": read["sha256"],
            "content": read["content"],
            "project_id": read["project_id"],
            "kind": read["kind"],
            "source": read["source"],
            "location": read["location"],
        }
    return snapshot


def _mcp_payload(result: Any, operation: str) -> dict[str, Any]:
    if result.is_error or not isinstance(result.structured_content, dict):
        raise RuntimeError(f"STDIO MCP {operation} failed")
    return result.structured_content


async def _run_stdio_flow(
    data_dir: Path,
    *,
    expected_manifest: list[dict[str, Any]],
    query: str,
    project_id: str,
    expected_source_ref: str,
    expected_content: str,
) -> dict[str, Any]:
    environment = os.environ.copy()
    environment["CONTEXT_HUB_DATA_DIR"] = str(data_dir)
    environment.pop("CONTEXT_HUB_WRITE_ENABLED", None)
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "context_hub.mcp_stdio"],
        env=environment,
        cwd=Path(__file__).resolve().parents[1],
    )
    started = time.perf_counter()
    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await asyncio.wait_for(session.initialize(), timeout=10)
            tools = await asyncio.wait_for(session.list_tools(), timeout=10)
            manifest = _mcp_payload(
                await asyncio.wait_for(
                    session.call_tool("context_get", {"op": "manifest"}), timeout=10
                ),
                "manifest",
            )
            search = _mcp_payload(
                await asyncio.wait_for(
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
                    timeout=10,
                ),
                "search",
            )
            if not search["items"]:
                raise RuntimeError("STDIO MCP project-scoped search returned no items")
            item = search["items"][0]
            read = _mcp_payload(
                await asyncio.wait_for(
                    session.call_tool(
                        "context_get",
                        {"op": "read", "ref": item["ref"], "max_chars": 4_000},
                    ),
                    timeout=10,
                ),
                "read",
            )

    return {
        "elapsed_ms": round((time.perf_counter() - started) * 1_000, 3),
        "checks": {
            "read_only_tool_surface": [tool.name for tool in tools.tools] == ["context_get"],
            "manifest_exact": manifest["projects"] == expected_manifest,
            "project_search_exact": item["project_id"] == project_id
            and item["source"]["ref"] == expected_source_ref,
            "search_mode_fts5_trigram": search["mode"] == "fts5-trigram",
            "read_content_exact": read["content"] == expected_content,
            "read_hash_exact": read["sha256"] == sha256_text(expected_content),
            "read_source_exact": read["project_id"] == project_id
            and read["source"]["ref"] == expected_source_ref,
        },
    }


def run() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="context-hub-multiproject-") as temporary:
        temporary_root = Path(temporary).resolve()
        data_dir = temporary_root / "hub-data"
        projects_root = temporary_root / "projects"
        documents_by_project: dict[str, dict[str, str]] = {}
        project_roots: dict[str, Path] = {}

        fixture_started = time.perf_counter()
        for spec in PROJECT_SPECS:
            project_id = spec["project_id"]
            project_root = projects_root / project_id
            documents = _fixture_documents(spec)
            _write_fixture(project_root, documents)
            documents_by_project[project_id] = documents
            project_roots[project_id] = project_root

        hub = ContextHub(data_dir)
        hub.initialize()
        registrations = [
            hub.register_project(spec["project_id"], project_roots[spec["project_id"]])
            for spec in PROJECT_SPECS
        ]
        fixture_setup_ms = (time.perf_counter() - fixture_started) * 1_000

        event_refs: dict[str, str] = {}
        for spec in PROJECT_SPECS:
            project_id = spec["project_id"]
            created = hub.put(
                action="append",
                kind="decision",
                content=f"{spec['event_marker']}，只属于 {project_id} 的合成事件。",
                project_id=project_id,
                source_type="acceptance",
                source_ref=f"synthetic:{project_id}:decision",
                event_id=EVENT_IDS[project_id],
                confirmed=True,
            )
            event_refs[project_id] = f"event:{created['event_id']}"

        history_old = hub.put(
            action="append",
            kind="status",
            content=f"{HISTORY_OLD_MARKER}，仅用于历史可见性验证。",
            project_id="cobalt",
            source_type="acceptance",
            source_ref="synthetic:cobalt:history-old",
            event_id=EVENT_IDS["history_old"],
            confirmed=True,
        )
        history_new = hub.put(
            action="supersede",
            kind="status",
            content=f"{HISTORY_NEW_MARKER}，仅用于当前状态验证。",
            project_id="cobalt",
            source_type="acceptance",
            source_ref="synthetic:cobalt:history-new",
            supersedes=history_old["event_id"],
            event_id=EVENT_IDS["history_new"],
            confirmed=True,
        )

        expected_manifest = [
            {"project_id": spec["project_id"], "files": ["AGENTS.md", "context/notes.md"]}
            for spec in PROJECT_SPECS
        ]
        manifest = hub.manifest()
        lexical_metrics, query_outcomes = _evaluate_queries(hub, event_refs)

        wrong_project_probes = 0
        wrong_project_empty = True
        for source_spec in PROJECT_SPECS:
            for target_spec in PROJECT_SPECS:
                if source_spec["project_id"] == target_spec["project_id"]:
                    continue
                wrong_project_probes += 1
                result = hub.search(
                    source_spec["stable_marker"],
                    project_id=target_spec["project_id"],
                    limit=3,
                    max_chars=600,
                )
                wrong_project_empty = wrong_project_empty and not result["items"]

        shared_scope_results = [
            hub.search(
                "共享评审线索",
                project_id=spec["project_id"],
                limit=5,
                max_chars=1_200,
            )
            for spec in PROJECT_SPECS
        ]
        shared_scope_isolated = all(
            result["items"]
            and all(item["project_id"] == spec["project_id"] for item in result["items"])
            for spec, result in zip(PROJECT_SPECS, shared_scope_results, strict=True)
        )

        pagination_item = _find_item(
            hub,
            PROJECT_SPECS[0]["page_marker"],
            project_id="aurora",
            source_ref="aurora:AGENTS.md",
        )
        pagination_read = _read_all(hub, pagination_item["ref"], max_chars=200)
        expected_pagination = documents_by_project["aurora"]["AGENTS.md"].split(
            "## 分页证据\n\n", 1
        )[1]
        expected_pagination = f"## 分页证据\n\n{expected_pagination}"
        pagination_exact = (
            pagination_read["pages"] > 1
            and pagination_read["content"] == expected_pagination
            and pagination_read["total_chars"] == len(expected_pagination)
            and pagination_read["sha256"] == sha256_text(expected_pagination)
        )

        old_item = _find_item(
            hub,
            EDIT_OLD_MARKER,
            project_id="aurora",
            source_ref="aurora:AGENTS.md",
        )
        aurora_agents = project_roots["aurora"] / "AGENTS.md"
        edited_content = documents_by_project["aurora"]["AGENTS.md"].replace(
            EDIT_OLD_MARKER, EDIT_NEW_MARKER
        )
        aurora_agents.write_text(edited_content, encoding="utf-8", newline="\n")
        documents_by_project["aurora"]["AGENTS.md"] = edited_content
        edit_started = time.perf_counter()
        hub.initialize()
        edit_sync_ms = (time.perf_counter() - edit_started) * 1_000
        edited_item = _find_item(
            hub,
            EDIT_NEW_MARKER,
            project_id="aurora",
            source_ref="aurora:AGENTS.md",
        )
        edit_sync_exact = (
            not hub.search(EDIT_OLD_MARKER, project_id="aurora")["items"]
            and edited_item["ref"] != old_item["ref"]
            and _ref_is_missing(hub, old_item["ref"])
        )

        deleted_item = _find_item(
            hub,
            PROJECT_SPECS[1]["english_marker"],
            project_id="bamboo",
            source_ref="bamboo:context/notes.md",
        )
        deleted_path = project_roots["bamboo"] / "context" / "notes.md"
        deleted_path.unlink()
        delete_started = time.perf_counter()
        hub.initialize()
        delete_sync_ms = (time.perf_counter() - delete_started) * 1_000
        delete_sync_exact = (
            not hub.search(PROJECT_SPECS[1]["english_marker"], project_id="bamboo")["items"]
            and _ref_is_missing(hub, deleted_item["ref"])
        )

        history_default = hub.search(
            HISTORY_OLD_MARKER, project_id="cobalt", kinds=["status"]
        )
        history_visible = hub.search(
            HISTORY_OLD_MARKER,
            project_id="cobalt",
            kinds=["status"],
            include_history=True,
        )
        current_visible = hub.search(
            HISTORY_NEW_MARKER, project_id="cobalt", kinds=["status"]
        )
        history_exact = (
            not history_default["items"]
            and len(history_visible["items"]) == 1
            and history_visible["items"][0]["ref"] == f"event:{history_old['event_id']}"
            and history_visible["items"][0]["superseded"] is True
            and len(current_visible["items"]) == 1
            and current_visible["items"][0]["ref"] == f"event:{history_new['event_id']}"
            and current_visible["items"][0]["superseded"] is False
        )

        identity_probes = [
            {
                "name": "edited-file",
                "query": EDIT_NEW_MARKER,
                "project_id": "aurora",
                "source_ref": "aurora:AGENTS.md",
            },
            {
                "name": "unchanged-file",
                "query": PROJECT_SPECS[2]["english_marker"],
                "project_id": "cobalt",
                "source_ref": "cobalt:context/notes.md",
            },
            {
                "name": "decision-event",
                "query": PROJECT_SPECS[0]["event_marker"],
                "project_id": "aurora",
                "expected_ref": event_refs["aurora"],
                "kinds": ["decision"],
            },
            {
                "name": "superseding-event",
                "query": HISTORY_NEW_MARKER,
                "project_id": "cobalt",
                "expected_ref": f"event:{history_new['event_id']}",
                "kinds": ["status"],
            },
        ]
        snapshot_before = _snapshot(hub, identity_probes)
        hub.remove_index_for_test()
        reindex_started = time.perf_counter()
        reindex_result = hub.reindex()
        reindex_ms = (time.perf_counter() - reindex_started) * 1_000
        snapshot_after = _snapshot(hub, identity_probes)
        reindex_identity = snapshot_before == snapshot_after

        doctor = hub.doctor()
        expected_counts = {
            "events": 5,
            "indexed_events": 5,
            "event_fts": 5,
            "sections": 11,
            "section_fts": 11,
            "projects": 3,
        }
        missing_source_warned = len(doctor["warnings"]) == 1 and any(
            "bamboo:context/notes.md" in warning for warning in doctor["warnings"]
        )

        stdio = asyncio.run(
            _run_stdio_flow(
                data_dir,
                expected_manifest=expected_manifest,
                query=PROJECT_SPECS[2]["english_marker"],
                project_id="cobalt",
                expected_source_ref="cobalt:context/notes.md",
                expected_content=documents_by_project["cobalt"]["context/notes.md"],
            )
        )

        generated_paths = [
            project_roots[spec["project_id"]] / relative_path
            for spec in PROJECT_SPECS
            for relative_path in documents_by_project[spec["project_id"]]
        ]
        source_boundary_exact = (
            _is_within(data_dir, temporary_root)
            and all(_is_within(root, temporary_root) for root in project_roots.values())
            and all(_is_within(path, temporary_root) for path in generated_paths)
        )

        checks = {
            "temporary_source_boundary": source_boundary_exact,
            "three_projects_registered": len(registrations) == 3
            and all(registration["ok"] for registration in registrations),
            "manifest_exact": manifest["projects"] == expected_manifest,
            "lexical_top3_recall_ge_85_percent": lexical_metrics["top3_recall"] >= 0.85,
            "query_modes_exact": all(outcome["mode_expected"] for outcome in query_outcomes),
            "project_filter_zero_cross_talk": all(
                outcome["project_isolated"] for outcome in query_outcomes
            )
            and shared_scope_isolated,
            "wrong_project_markers_absent": wrong_project_empty,
            "kind_filter_exact": all(outcome["kind_isolated"] for outcome in query_outcomes),
            "pagination_content_and_hash_exact": pagination_exact,
            "source_edit_invalidates_old_ref": edit_sync_exact,
            "source_delete_removes_index_entry": delete_sync_exact,
            "missing_registered_source_warned": missing_source_warned,
            "supersede_history_exact": history_exact,
            "reindex_ids_content_hashes_exact": reindex_identity,
            "reindex_count_exact": reindex_result
            == {"ok": True, "events": 5, "sections": 11},
            "doctor_exact": doctor["ok"] and doctor["counts"] == expected_counts,
            **{f"stdio_{name}": value for name, value in stdio["checks"].items()},
        }

        return {
            "ok": all(checks.values()),
            "synthetic_only": True,
            "source_boundary": {
                "temporary_root": True,
                "external_fact_inputs": 0,
                "generated_registered_files": 6,
                "available_files_after_delete_probe": 5,
            },
            "dataset": {
                "projects": 3,
                "events": 5,
                "indexed_sections_after_mutations": 11,
                "lexical_query_cases": lexical_metrics["cases"],
                "wrong_project_probes": wrong_project_probes,
                "shared_heading_scope_probes": len(shared_scope_results),
            },
            "retrieval": lexical_metrics,
            "timings": {
                "fixture_setup_and_registration_ms": round(fixture_setup_ms, 3),
                "source_edit_sync_ms": round(edit_sync_ms, 3),
                "source_delete_sync_ms": round(delete_sync_ms, 3),
                "clean_reindex_ms": round(reindex_ms, 3),
                "stdio_manifest_search_read_ms": stdio["elapsed_ms"],
            },
            "checks": checks,
            "doctor_counts": doctor["counts"],
            "doctor_warning_count": len(doctor["warnings"]),
            "notes": [
                "Every fact source was generated inside one temporary directory and removed afterward.",
                "The retrieval score is a fixed lexical acceptance set, not a semantic or model-quality claim.",
                "One registered synthetic file is intentionally deleted to verify sync removal and doctor warnings.",
                "The STDIO probe launches the installed local context_hub.mcp_stdio entry point in read-only mode.",
            ],
        }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = run()
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8", newline="\n")
    print(encoded, end="")
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

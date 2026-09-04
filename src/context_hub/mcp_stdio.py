"""Minimal MCP STDIO adapter for clients that can launch local servers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from .hub import ContextHub


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().casefold() in {"1", "true", "yes", "on"}


def build_server(
    data_dir: str | os.PathLike[str] | None = None,
    *,
    write_enabled: bool | None = None,
) -> MCPServer:
    hub = ContextHub(data_dir)
    hub.initialize()
    writes = hub.write_enabled if write_enabled is None else write_enabled
    if _env_flag("CONTEXT_HUB_WRITE_ENABLED"):
        writes = True

    server = MCPServer(
        name="context-hub",
        title="Context Hub",
        description="Local-first retrieval over explicit JSONL and Markdown fact sources.",
        instructions=(
            "Call context_get only when prior preferences, decisions, constraints, facts, or project status "
            "materially affect the answer; ordinary standalone questions need no lookup. Search first with "
            "limit=3 and max_chars=600, then read a stable ref only when the original text is needed. Ignore "
            "superseded records unless history was explicitly requested. Call context_put only after the user "
            "explicitly asks for or confirms that exact write. Writes are hidden unless enabled."
        ),
        version="0.1.0",
    )

    @server.tool(name="context_get", description="Search, read a stable ref, or inspect the public manifest.", structured_output=True)
    def context_get(
        op: Literal["search", "read", "manifest"],
        query: str | None = None,
        ref: str | None = None,
        cursor: str | None = None,
        project_id: str | None = None,
        kinds: list[Literal["preference", "decision", "constraint", "fact", "status"]] | None = None,
        limit: int = 3,
        max_chars: int = 600,
        include_history: bool = False,
    ) -> dict[str, Any]:
        if op == "search":
            return hub.search(
                query or "",
                project_id=project_id,
                kinds=kinds,
                limit=limit,
                max_chars=max_chars,
                include_history=include_history,
            )
        if op == "read":
            return hub.read(ref, cursor=cursor, max_chars=max_chars)
        return hub.manifest()

    if writes:

        @server.tool(name="context_put", description="Append one explicitly confirmed immutable event.", structured_output=True)
        def context_put(
            action: Literal["append", "supersede", "tombstone"],
            kind: Literal["preference", "decision", "constraint", "fact", "status"],
            content: str,
            source_ref: str,
            project_id: str | None = None,
            supersedes: str | None = None,
            event_id: str | None = None,
        ) -> dict[str, Any]:
            return hub.put(
                action=action,
                kind=kind,
                content=content,
                project_id=project_id,
                source_type="mcp",
                source_ref=source_ref,
                supersedes=supersedes,
                event_id=event_id,
                confirmed=True,
            )

    return server


def main() -> None:
    data_dir = os.environ.get("CONTEXT_HUB_DATA_DIR")
    build_server(Path(data_dir) if data_dir else None).run("stdio")


if __name__ == "__main__":
    main()

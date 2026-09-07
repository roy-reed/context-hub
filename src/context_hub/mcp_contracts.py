"""Shared, compact output contracts for Context Hub MCP tools."""

from __future__ import annotations

from typing import Any


def _invocation_marker_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"active": {"const": True}},
        "required": [
            "active",
            "status",
            "transport",
            "request_id",
            "source_count",
            "source_types",
            "project_ids",
        ],
    }


CONTEXT_GET_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "op": {"enum": ["search", "read", "manifest"]},
        "items": {"type": "array", "items": {"type": "object"}},
        "content": {"type": "string"},
        "next_cursor": {"type": ["string", "null"]},
        "projects": {"type": "array", "items": {"type": "object"}},
        "context_hub": _invocation_marker_schema(),
    },
    "required": ["op", "context_hub"],
}


CONTEXT_PUT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"enum": ["existing", "persisted", "created"]},
        "event_id": {"type": "string"},
        "content_sha256": {"type": "string"},
        "index_state": {"enum": ["unchanged", "pending_reindex", "current"]},
        "warning": {"type": "string"},
        "context_hub": _invocation_marker_schema(),
    },
    "required": [
        "status",
        "event_id",
        "content_sha256",
        "index_state",
        "context_hub",
    ],
}

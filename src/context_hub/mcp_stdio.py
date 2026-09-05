"""Minimal MCP STDIO adapter for clients that can launch local servers."""

from __future__ import annotations

import importlib
import json
import os
import sys
import uuid
from importlib.machinery import ModuleSpec, PathFinder
from pathlib import Path
from types import ModuleType
from typing import Any

from .hub import ContextHub


def _prepare_mcp_namespace() -> None:
    """Skip MCP's eager convenience imports for the server subprocess.

    MCP 2.1.1 imports its client and every server transport from package
    \`\`__init__\`\` modules. A STDIO-only process needs neither the client nor
    the HTTP stack, so expose the installed package paths as namespaces before
    importing the exact official protocol and transport modules below.

    If another caller has already imported MCP (for example the contract
    tests), preserve that normal package unchanged.
    """

    if "mcp" in sys.modules:
        return

    package_spec = PathFinder.find_spec("mcp")
    locations = package_spec.submodule_search_locations if package_spec else None
    if not locations:
        raise ImportError("The installed 'mcp' package could not be located")

    package_path = Path(next(iter(locations)))
    mcp_module = ModuleType("mcp")
    mcp_module.__file__ = str(package_path / "__init__.py")
    mcp_module.__package__ = "mcp"
    mcp_module.__path__ = [str(package_path)]
    mcp_module.__spec__ = ModuleSpec("mcp", loader=None, is_package=True)

    server_path = package_path / "server"
    server_module = ModuleType("mcp.server")
    server_module.__file__ = str(server_path / "__init__.py")
    server_module.__package__ = "mcp.server"
    server_module.__path__ = [str(server_path)]
    server_module.__spec__ = ModuleSpec("mcp.server", loader=None, is_package=True)

    sys.modules["mcp"] = mcp_module
    sys.modules["mcp.server"] = server_module
    mcp_module.server = server_module

anyio: Any = None
mcp_types: Any = None
SessionMessage: Any = None
stdio_server: Any = None
HANDSHAKE_PROTOCOL_VERSIONS: tuple[str, ...] = ()
LATEST_HANDSHAKE_VERSION = ""


def _load_mcp_runtime(*, fast_startup: bool) -> None:
    """Load MCP dependencies, using the narrow path only for the CLI process."""
    global anyio
    global mcp_types
    global SessionMessage
    global stdio_server
    global HANDSHAKE_PROTOCOL_VERSIONS
    global LATEST_HANDSHAKE_VERSION

    if mcp_types is not None:
        return

    if fast_startup:
        _prepare_mcp_namespace()

    anyio = importlib.import_module("anyio")
    mcp_types = importlib.import_module("mcp_types")
    version_module = importlib.import_module("mcp_types.version")
    session_module = importlib.import_module("mcp.shared.message")
    stdio_module = importlib.import_module("mcp.server.stdio")
    SessionMessage = session_module.SessionMessage
    stdio_server = stdio_module.stdio_server
    HANDSHAKE_PROTOCOL_VERSIONS = tuple(version_module.HANDSHAKE_PROTOCOL_VERSIONS)
    LATEST_HANDSHAKE_VERSION = version_module.LATEST_HANDSHAKE_VERSION


INSTRUCTIONS = (
    "Call context_get only when prior preferences, decisions, constraints, facts, or project status "
    "materially affect the answer; ordinary standalone questions need no lookup. Search first with "
    "limit=3 and max_chars=600, then read a stable ref only when the original text is needed. Ignore "
    "superseded records unless history was explicitly requested. Call context_put only after the user "
    "explicitly asks for or confirms that exact write. Writes are hidden unless enabled. Every tool "
    "response includes context_hub.status, transport, request_id, and a bounded source summary so the "
    "client can show that Context Hub actually ran."
)
KINDS = {"preference", "decision", "constraint", "fact", "status"}

CONTEXT_GET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "op": {"type": "string", "enum": ["search", "read", "manifest"]},
        "query": {"type": ["string", "null"], "default": None},
        "ref": {"type": ["string", "null"], "default": None},
        "cursor": {"type": ["string", "null"], "default": None},
        "project_id": {"type": ["string", "null"], "default": None},
        "kinds": {
            "type": ["array", "null"],
            "items": {"type": "string", "enum": sorted(KINDS)},
            "default": None,
        },
        "limit": {"type": "integer", "default": 3},
        "max_chars": {"type": "integer", "default": 600},
        "include_history": {"type": "boolean", "default": False},
    },
    "required": ["op"],
}

CONTEXT_PUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["append", "supersede", "tombstone"]},
        "kind": {"type": "string", "enum": sorted(KINDS)},
        "content": {"type": "string"},
        "source_ref": {"type": "string"},
        "project_id": {"type": ["string", "null"], "default": None},
        "supersedes": {"type": ["string", "null"], "default": None},
        "event_id": {"type": ["string", "null"], "default": None},
    },
    "required": ["action", "kind", "content", "source_ref"],
}

OUTPUT_SCHEMA: dict[str, Any] = {"type": "object", "additionalProperties": True}


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().casefold() in {"1", "true", "yes", "on"}


def _optional_string(arguments: dict[str, Any], name: str) -> str | None:
    value = arguments.get(name)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{name} must be a string or null")
    return value


def _required_string(arguments: dict[str, Any], name: str) -> str:
    if name not in arguments:
        raise ValueError(f"{name} is required")
    value = arguments[name]
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def _integer(arguments: dict[str, Any], name: str, default: int) -> int:
    value = arguments.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _json_result(payload: dict[str, Any]) -> mcp_types.CallToolResult:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text=text)],
        structured_content=payload,
        is_error=False,
    )


def _error_result(
    error: Exception | str,
    *,
    marker: dict[str, Any] | None = None,
) -> mcp_types.CallToolResult:
    payload: dict[str, Any] = {"error": str(error)}
    if marker is not None:
        payload["context_hub"] = marker
    return mcp_types.CallToolResult(
        content=[
            mcp_types.TextContent(
                type="text",
                text=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            )
        ],
        structured_content=payload,
        is_error=True,
    )


class ContextHubMCPServer:
    """Small MCP dispatcher over the official protocol types and STDIO transport."""

    def __init__(self, hub: ContextHub, *, writes: bool, transport: str = "stdio") -> None:
        _load_mcp_runtime(fast_startup=False)
        self.hub = hub
        self.writes = writes
        self.transport = transport
        self.instructions = INSTRUCTIONS
        self._tools = [
            mcp_types.Tool(
                name="context_get",
                description=(
                    "Search, read a stable ref, or inspect the public manifest. Responses include "
                    "a context_hub invocation marker and source summary."
                ),
                input_schema=CONTEXT_GET_SCHEMA,
                output_schema=OUTPUT_SCHEMA,
            )
        ]
        if writes:
            self._tools.append(
                mcp_types.Tool(
                    name="context_put",
                    description="Append one explicitly confirmed immutable event.",
                    input_schema=CONTEXT_PUT_SCHEMA,
                    output_schema=OUTPUT_SCHEMA,
                )
            )

        self._initialize_accepted = False

    async def list_tools(self) -> list[mcp_types.Tool]:
        return list(self._tools)

    async def call_tool(self, name: str, arguments: dict[str, Any] | None) -> mcp_types.CallToolResult:
        request_id = f"ch_{uuid.uuid4().hex[:12]}"
        operation = name
        try:
            values = arguments or {}
            if not isinstance(values, dict):
                raise ValueError("tool arguments must be an object")
            if name == "context_get":
                operation = str(values.get("op", name))
                payload = self._context_get(values)
                payload["context_hub"] = self._invocation_marker(
                    payload,
                    operation=operation,
                    request_id=request_id,
                )
                return _json_result(payload)
            if name == "context_put" and self.writes:
                operation = str(values.get("action", name))
                payload = self._context_put(values)
                payload["context_hub"] = self._invocation_marker(
                    payload,
                    operation=operation,
                    request_id=request_id,
                    source_types={"mcp"},
                    project_ids={values["project_id"]} if values.get("project_id") else set(),
                    source_count=1,
                )
                return _json_result(payload)
            raise ValueError(f"Unknown tool: {name}")
        except Exception as exc:
            marker = self._invocation_marker(
                {},
                operation=operation,
                request_id=request_id,
                status="error",
            )
            return _error_result(exc, marker=marker)

    def _invocation_marker(
        self,
        payload: dict[str, Any],
        *,
        operation: str,
        request_id: str,
        status: str = "invoked",
        source_types: set[str] | None = None,
        project_ids: set[str] | None = None,
        source_count: int | None = None,
    ) -> dict[str, Any]:
        """Return a small, client-visible proof that this adapter handled a call."""
        derived_source_types = set(source_types or ())
        derived_project_ids = set(project_ids or ())
        count = 0 if source_count is None else source_count

        items = payload.get("items")
        candidates = items if isinstance(items, list) else [payload]
        if source_count is None:
            count = len(items) if isinstance(items, list) else int(isinstance(payload.get("source"), dict))
        for item in candidates:
            if not isinstance(item, dict):
                continue
            source = item.get("source")
            if isinstance(source, dict) and isinstance(source.get("type"), str):
                derived_source_types.add(source["type"])
            elif item.get("kind") == "markdown":
                derived_source_types.add("markdown")
            project_id = item.get("project_id")
            if isinstance(project_id, str):
                derived_project_ids.add(project_id)

        if payload.get("op") == "manifest":
            projects = payload.get("projects")
            if isinstance(projects, list):
                count = sum(
                    len(project.get("files", []))
                    for project in projects
                    if isinstance(project, dict) and isinstance(project.get("files"), list)
                )
                if count:
                    derived_source_types.add("markdown")
                derived_project_ids.update(
                    project["project_id"]
                    for project in projects
                    if isinstance(project, dict) and isinstance(project.get("project_id"), str)
                )

        return {
            "active": True,
            "status": status,
            "server": "context-hub",
            "transport": self.transport,
            "operation": operation,
            "request_id": request_id,
            "source_count": count,
            "source_types": sorted(derived_source_types),
            "project_ids": sorted(derived_project_ids),
        }

    def _context_get(self, arguments: dict[str, Any]) -> dict[str, Any]:
        op = _required_string(arguments, "op")
        if op not in {"search", "read", "manifest"}:
            raise ValueError("op must be one of: search, read, manifest")

        query = _optional_string(arguments, "query")
        ref = _optional_string(arguments, "ref")
        cursor = _optional_string(arguments, "cursor")
        project_id = _optional_string(arguments, "project_id")
        kinds = arguments.get("kinds")
        if kinds is not None:
            if not isinstance(kinds, list) or any(
                not isinstance(kind, str) or kind not in KINDS for kind in kinds
            ):
                raise ValueError("kinds must be an array of supported kind names or null")
        limit = _integer(arguments, "limit", 3)
        max_chars = _integer(arguments, "max_chars", 600)
        include_history = arguments.get("include_history", False)
        if not isinstance(include_history, bool):
            raise ValueError("include_history must be a boolean")

        if op == "search":
            return self.hub.search(
                query or "",
                project_id=project_id,
                kinds=kinds,
                limit=limit,
                max_chars=max_chars,
                include_history=include_history,
            )
        if op == "read":
            return self.hub.read(ref, cursor=cursor, max_chars=max_chars)
        return self.hub.manifest()

    def _context_put(self, arguments: dict[str, Any]) -> dict[str, Any]:
        action = _required_string(arguments, "action")
        if action not in {"append", "supersede", "tombstone"}:
            raise ValueError("action must be one of: append, supersede, tombstone")
        kind = _required_string(arguments, "kind")
        if kind not in KINDS:
            raise ValueError("kind must be a supported kind name")

        return self.hub.put(
            action=action,
            kind=kind,
            content=_required_string(arguments, "content"),
            project_id=_optional_string(arguments, "project_id"),
            source_type="mcp",
            source_ref=_required_string(arguments, "source_ref"),
            supersedes=_optional_string(arguments, "supersedes"),
            event_id=_optional_string(arguments, "event_id"),
            confirmed=True,
        )

    @staticmethod
    def _payload(result: Any) -> dict[str, Any]:
        return result.model_dump(by_alias=True, mode="json", exclude_none=True)

    @staticmethod
    def _rpc_error(
        request_id: int | str | None,
        code: int,
        message: str,
        data: Any = None,
    ) -> mcp_types.JSONRPCError:
        return mcp_types.JSONRPCError(
            jsonrpc=mcp_types.JSONRPC_VERSION,
            id=request_id,
            error=mcp_types.ErrorData(code=code, message=message, data=data),
        )

    async def _dispatch_request(
        self,
        request: mcp_types.JSONRPCRequest,
    ) -> mcp_types.JSONRPCResponse | mcp_types.JSONRPCError:
        try:
            if request.method == "initialize":
                params = mcp_types.InitializeRequestParams.model_validate(
                    request.params or {},
                    by_name=False,
                )
                negotiated = (
                    params.protocol_version
                    if params.protocol_version in HANDSHAKE_PROTOCOL_VERSIONS
                    else LATEST_HANDSHAKE_VERSION
                )
                result = mcp_types.InitializeResult(
                    protocol_version=negotiated,
                    capabilities=mcp_types.ServerCapabilities(
                        tools=mcp_types.ToolsCapability(list_changed=False)
                    ),
                    server_info=mcp_types.Implementation(
                        name="context-hub",
                        title="Context Hub",
                        version="0.1.0",
                        description="Local-first retrieval over explicit JSONL and Markdown fact sources.",
                    ),
                    instructions=self.instructions,
                )
                self._initialize_accepted = True
            elif request.method == "ping":
                result = mcp_types.EmptyResult()
            elif not self._initialize_accepted:
                return self._rpc_error(
                    request.id,
                    mcp_types.INVALID_PARAMS,
                    "Invalid request parameters",
                    "",
                )
            elif request.method == "tools/list":
                result = mcp_types.ListToolsResult(tools=await self.list_tools())
            elif request.method == "tools/call":
                params = mcp_types.CallToolRequestParams.model_validate(
                    request.params or {},
                    by_name=False,
                )
                result = await self.call_tool(params.name, params.arguments)
            else:
                return self._rpc_error(
                    request.id,
                    mcp_types.METHOD_NOT_FOUND,
                    "Method not found",
                    request.method,
                )
            return mcp_types.JSONRPCResponse(
                jsonrpc=mcp_types.JSONRPC_VERSION,
                id=request.id,
                result=self._payload(result),
            )
        except (TypeError, ValueError) as exc:
            return self._rpc_error(
                request.id,
                mcp_types.INVALID_PARAMS,
                "Invalid request parameters",
                str(exc),
            )
        except Exception as exc:
            return self._rpc_error(
                request.id,
                mcp_types.INTERNAL_ERROR,
                "Internal error",
                str(exc),
            )

    async def run_stdio_async(self) -> None:
        async with stdio_server() as (read_stream, write_stream):
            async with read_stream, write_stream:
                async for incoming in read_stream:
                    if isinstance(incoming, Exception):
                        response: mcp_types.JSONRPCError | None = self._rpc_error(
                            None,
                            mcp_types.PARSE_ERROR,
                            "Parse error",
                        )
                    else:
                        message = incoming.message
                        response = None
                        if isinstance(message, mcp_types.JSONRPCRequest):
                            response = await self._dispatch_request(message)
                        elif (
                            isinstance(message, mcp_types.JSONRPCNotification)
                            and message.method == "notifications/initialized"
                        ):
                            self._initialize_accepted = True
                    if response is not None:
                        await write_stream.send(SessionMessage(response))

    def run(self, transport: str = "stdio") -> None:
        if transport != "stdio":
            raise ValueError("Context Hub only supports the stdio transport")
        anyio.run(self.run_stdio_async)


def build_server(
    data_dir: str | os.PathLike[str] | None = None,
    *,
    write_enabled: bool | None = None,
    transport: str = "stdio",
) -> ContextHubMCPServer:
    hub = ContextHub(data_dir)
    hub.initialize()
    writes = hub.write_enabled if write_enabled is None else write_enabled
    if _env_flag("CONTEXT_HUB_WRITE_ENABLED"):
        writes = True
    return ContextHubMCPServer(hub, writes=writes, transport=transport)


def main() -> None:
    _load_mcp_runtime(fast_startup=True)
    data_dir = os.environ.get("CONTEXT_HUB_DATA_DIR")
    build_server(Path(data_dir) if data_dir else None).run("stdio")


if __name__ == "__main__":
    main()

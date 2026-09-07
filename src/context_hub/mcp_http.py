"""Streamable HTTP MCP adapter for remote-capable clients such as ChatGPT."""

from __future__ import annotations

import argparse
from copy import deepcopy
import ipaddress
import os
from pathlib import Path
from typing import Annotated, Any, Literal, Sequence

from mcp_types import CallToolResult
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import RootModel

from . import __version__
from .mcp_contracts import CONTEXT_GET_OUTPUT_SCHEMA, CONTEXT_PUT_OUTPUT_SCHEMA
from .mcp_stdio import INSTRUCTIONS, ContextHubMCPServer, build_server


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_PATH = "/mcp"
DEFAULT_MAX_REQUEST_BYTES = 1024 * 1024


class _ContextGetOutput(RootModel[dict[str, Any]]):
    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema: Any, handler: Any) -> dict[str, Any]:
        return deepcopy(CONTEXT_GET_OUTPUT_SCHEMA)


class _ContextPutOutput(RootModel[dict[str, Any]]):
    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema: Any, handler: Any) -> dict[str, Any]:
        return deepcopy(CONTEXT_PUT_OUTPUT_SCHEMA)


def _is_loopback(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _normalize_path(value: str) -> str:
    if not value.startswith("/") or value == "/" or ".." in value or "?" in value or "#" in value:
        raise argparse.ArgumentTypeError("path must be an absolute non-root URL path without '..', '?' or '#'")
    return value.rstrip("/")


def _security_settings(
    host: str,
    port: int,
    *,
    allowed_hosts: Sequence[str] = (),
    allowed_origins: Sequence[str] = (),
) -> TransportSecuritySettings:
    hosts = {
        host,
        f"{host}:{port}",
        "127.0.0.1",
        "127.0.0.1:*",
        "localhost",
        "localhost:*",
        "[::1]",
        "[::1]:*",
        *allowed_hosts,
    }
    origins = {
        "http://127.0.0.1:*",
        "http://localhost:*",
        "https://chatgpt.com",
        *allowed_origins,
    }
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=sorted(hosts),
        allowed_origins=sorted(origins),
    )


def build_http_server(
    data_dir: str | os.PathLike[str] | None = None,
    *,
    write_enabled: bool | None = None,
) -> tuple[MCPServer, ContextHubMCPServer]:
    """Build the official SDK server while sharing the audited core dispatcher."""
    dispatcher = build_server(
        data_dir,
        write_enabled=write_enabled,
        transport="streamable-http",
    )
    server = MCPServer(
        name="context-hub",
        title="Context Hub",
        description="Local-first retrieval over explicit JSONL and Markdown fact sources.",
        instructions=INSTRUCTIONS,
        version=__version__,
    )

    @server.tool(
        name="context_get",
        description=(
            "Search, read a stable ref, or inspect the public manifest. Every response includes "
            "context_hub status, transport, request id, and source summary."
        ),
        structured_output=True,
    )
    async def context_get(
        op: Literal["search", "read", "manifest"],
        query: str | None = None,
        ref: str | None = None,
        cursor: str | None = None,
        project_id: str | None = None,
        kinds: list[str] | None = None,
        limit: int = 3,
        max_chars: int = 600,
        include_history: bool = False,
    ) -> Annotated[CallToolResult, _ContextGetOutput]:
        return await dispatcher.call_tool(
            "context_get",
            {
                "op": op,
                "query": query,
                "ref": ref,
                "cursor": cursor,
                "project_id": project_id,
                "kinds": kinds,
                "limit": limit,
                "max_chars": max_chars,
                "include_history": include_history,
            },
        )

    if dispatcher.writes:

        @server.tool(
            name="context_put",
            description="Append one explicitly confirmed immutable event and report its source.",
            structured_output=True,
        )
        async def context_put(
            action: Literal["append", "supersede", "tombstone"],
            kind: Literal["preference", "decision", "constraint", "fact", "status"],
            content: str,
            source_ref: str,
            project_id: str | None = None,
            supersedes: str | None = None,
            event_id: str | None = None,
        ) -> Annotated[CallToolResult, _ContextPutOutput]:
            return await dispatcher.call_tool(
                "context_put",
                {
                    "action": action,
                    "kind": kind,
                    "content": content,
                    "source_ref": source_ref,
                    "project_id": project_id,
                    "supersedes": supersedes,
                    "event_id": event_id,
                },
            )

    return server, dispatcher


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="context-hub-mcp-http")
    parser.add_argument("--data-dir", help="Context Hub data root")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--path", type=_normalize_path, default=DEFAULT_PATH)
    parser.add_argument(
        "--allow-public-bind",
        action="store_true",
        help="acknowledge binding to a non-loopback interface",
    )
    parser.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        help="additional exact Host header or host:* pattern (for a trusted HTTPS proxy)",
    )
    parser.add_argument(
        "--allowed-origin",
        action="append",
        default=[],
        help="additional exact Origin header or scheme://host:* pattern",
    )
    parser.add_argument(
        "--max-request-bytes",
        type=int,
        default=DEFAULT_MAX_REQUEST_BYTES,
        help="maximum JSON request body size",
    )
    return parser


def run(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if not 1 <= args.port <= 65535:
        raise SystemExit("port must be between 1 and 65535")
    if not 1024 <= args.max_request_bytes <= 16 * 1024 * 1024:
        raise SystemExit("max-request-bytes must be between 1024 and 16777216")
    if not _is_loopback(args.host) and not args.allow_public_bind:
        raise SystemExit("non-loopback bind requires --allow-public-bind")

    data_dir = args.data_dir or os.environ.get("CONTEXT_HUB_DATA_DIR")
    server, _ = build_http_server(Path(data_dir) if data_dir else None)
    server.run(
        "streamable-http",
        host=args.host,
        port=args.port,
        streamable_http_path=args.path,
        json_response=True,
        stateless_http=True,
        max_request_body_size=args.max_request_bytes,
        transport_security=_security_settings(
            args.host,
            args.port,
            allowed_hosts=args.allowed_host,
            allowed_origins=args.allowed_origin,
        ),
    )


def main() -> None:
    run()


if __name__ == "__main__":
    main()

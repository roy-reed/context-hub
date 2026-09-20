"""Streamable HTTP MCP adapter for remote-capable clients such as ChatGPT."""

from __future__ import annotations

import argparse
from copy import deepcopy
import hmac
import ipaddress
import os
from pathlib import Path
import re
from typing import Annotated, Any, Literal, Sequence

from mcp_types import CallToolResult
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import RootModel
import uvicorn

from . import __version__
from .mcp_contracts import CONTEXT_GET_OUTPUT_SCHEMA, CONTEXT_PUT_OUTPUT_SCHEMA
from .mcp_stdio import INSTRUCTIONS, ContextHubMCPServer, build_server


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_PATH = "/mcp"
DEFAULT_MAX_REQUEST_BYTES = 1024 * 1024
DEFAULT_AUTH_TOKEN_ENV = "CONTEXT_HUB_HTTP_BEARER_TOKEN"
MIN_AUTH_TOKEN_CHARS = 32
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class BearerAuthMiddleware:
    """Require one exact bearer token without logging or exposing its value."""

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self.expected = f"Bearer {token}".encode("utf-8")

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        supplied = next(
            (value for name, value in scope.get("headers", ()) if name.lower() == b"authorization"),
            b"",
        )
        if hmac.compare_digest(supplied, self.expected):
            await self.app(scope, receive, send)
            return

        if scope.get("type") == "websocket":
            await send({"type": "websocket.close", "code": 4401})
            return
        body = b'{"error":"unauthorized"}'
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"www-authenticate", b"Bearer"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


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
        "https://127.0.0.1:*",
        "https://localhost:*",
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
            description="Append one explicitly confirmed event and report its source.",
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
        help="acknowledge binding to a non-loopback interface (authentication still required)",
    )
    parser.add_argument(
        "--auth-token-env",
        default=DEFAULT_AUTH_TOKEN_ENV,
        help="environment variable containing a bearer token (never pass the token on the command line)",
    )
    parser.add_argument("--tls-cert", help="PEM certificate for direct HTTPS")
    parser.add_argument("--tls-key", help="PEM private key for direct HTTPS")
    parser.add_argument(
        "--allow-insecure-http",
        action="store_true",
        help="allow public plain HTTP only when TLS is terminated by a trusted reverse proxy",
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


def _runtime_security(args: argparse.Namespace) -> tuple[str | None, Path | None, Path | None]:
    if _ENV_NAME_RE.fullmatch(args.auth_token_env) is None:
        raise SystemExit("auth-token-env must be a valid environment variable name")
    token = os.environ.get(args.auth_token_env, "").strip() or None
    if token is not None and len(token) < MIN_AUTH_TOKEN_CHARS:
        raise SystemExit(f"bearer token must contain at least {MIN_AUTH_TOKEN_CHARS} characters")

    if bool(args.tls_cert) != bool(args.tls_key):
        raise SystemExit("tls-cert and tls-key must be supplied together")
    certificate = Path(args.tls_cert).expanduser().resolve() if args.tls_cert else None
    private_key = Path(args.tls_key).expanduser().resolve() if args.tls_key else None
    for label, path in (("tls-cert", certificate), ("tls-key", private_key)):
        if path is not None and not path.is_file():
            raise SystemExit(f"{label} must name an existing file")

    if not _is_loopback(args.host):
        if not args.allow_public_bind:
            raise SystemExit("non-loopback bind requires --allow-public-bind")
        if token is None:
            raise SystemExit(
                f"non-loopback bind requires a bearer token in {args.auth_token_env}"
            )
        if certificate is None and not args.allow_insecure_http:
            raise SystemExit(
                "non-loopback bind requires --tls-cert/--tls-key, or --allow-insecure-http "
                "behind a trusted TLS reverse proxy"
            )
    return token, certificate, private_key


def run(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if not 1 <= args.port <= 65535:
        raise SystemExit("port must be between 1 and 65535")
    if not 1024 <= args.max_request_bytes <= 16 * 1024 * 1024:
        raise SystemExit("max-request-bytes must be between 1024 and 16777216")
    token, certificate, private_key = _runtime_security(args)

    data_dir = args.data_dir or os.environ.get("CONTEXT_HUB_DATA_DIR")
    server, _ = build_http_server(Path(data_dir) if data_dir else None)
    app: Any = server.streamable_http_app(
        host=args.host,
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
    if token is not None:
        app = BearerAuthMiddleware(app, token)
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        ssl_certfile=str(certificate) if certificate else None,
        ssl_keyfile=str(private_key) if private_key else None,
        server_header=False,
    )


def main() -> None:
    run()


if __name__ == "__main__":
    main()

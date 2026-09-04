"""Command-line adapter for local administration and deterministic checks."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .errors import ContextHubError
from .hub import ALLOWED_ACTIONS, ALLOWED_KINDS, ContextHub


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="context-hub")
    parser.add_argument("--data-dir", help="Context Hub data root (default: CONTEXT_HUB_DATA_DIR or LocalAppData)")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="initialize an empty local data root")
    init.add_argument("--enable-writes", action="store_true", help="persistently enable MCP write-tool registration")

    register = commands.add_parser("register-project", help="register allow-listed Markdown sources")
    register.add_argument("project_id")
    register.add_argument("root")
    register.add_argument("--file", action="append", dest="files")

    search = commands.add_parser("search", help="search the derived index")
    search.add_argument("query")
    search.add_argument("--project-id")
    search.add_argument("--kind", action="append", dest="kinds", choices=sorted(ALLOWED_KINDS))
    search.add_argument("--limit", type=int, default=3)
    search.add_argument("--max-chars", type=int, default=600)
    search.add_argument("--include-history", action="store_true")

    read = commands.add_parser("read", help="read a stable reference in bounded chunks")
    read.add_argument("ref", nargs="?")
    read.add_argument("--cursor")
    read.add_argument("--max-chars", type=int, default=600)

    put = commands.add_parser("put", help="append an explicitly confirmed event")
    put.add_argument("--confirm-write", action="store_true", required=True)
    put.add_argument("--action", choices=sorted(ALLOWED_ACTIONS), required=True)
    put.add_argument("--kind", choices=sorted(ALLOWED_KINDS), required=True)
    put.add_argument("--content", required=True)
    put.add_argument("--project-id")
    put.add_argument("--source-type", default="cli")
    put.add_argument("--source-ref", required=True)
    put.add_argument("--supersedes")
    put.add_argument("--event-id")

    commands.add_parser("manifest", help="show the public project manifest")
    doctor = commands.add_parser("doctor", help="validate fact sources and the derived index")
    doctor.add_argument("--reindex", action="store_true", help="rebuild the derived index before validation")
    commands.add_parser("reindex", help="rebuild SQLite solely from fact sources")
    backup = commands.add_parser("backup", help="archive only authoritative files with hashes")
    backup.add_argument("--destination")
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    hub = ContextHub(args.data_dir)
    try:
        if args.command == "init":
            result = hub.initialize(write_enabled=args.enable_writes)
        elif args.command == "register-project":
            result = hub.register_project(args.project_id, args.root, args.files)
        elif args.command == "search":
            result = hub.search(
                args.query,
                project_id=args.project_id,
                kinds=args.kinds,
                limit=args.limit,
                max_chars=args.max_chars,
                include_history=args.include_history,
            )
        elif args.command == "read":
            result = hub.read(args.ref, cursor=args.cursor, max_chars=args.max_chars)
        elif args.command == "put":
            result = hub.put(
                action=args.action,
                kind=args.kind,
                content=args.content,
                project_id=args.project_id,
                source_type=args.source_type,
                source_ref=args.source_ref,
                supersedes=args.supersedes,
                event_id=args.event_id,
                confirmed=args.confirm_write,
            )
        elif args.command == "manifest":
            result = hub.manifest()
        elif args.command == "doctor":
            result = hub.doctor(reindex=args.reindex)
        elif args.command == "reindex":
            result = hub.reindex()
        elif args.command == "backup":
            result = hub.backup(args.destination)
        else:
            raise AssertionError(f"unhandled command: {args.command}")
        _print_json(result)
        if args.command == "doctor" and not result["ok"]:
            return 2
        return 0
    except (ContextHubError, FileNotFoundError, PermissionError) as exc:
        _print_json({"ok": False, "error": str(exc), "error_type": type(exc).__name__})
        return 2


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()

"""Command-line adapter for local administration and deterministic checks."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .errors import ContextHubError
from .hub import ALLOWED_ACTIONS, ALLOWED_KINDS, ContextHub
from .loop import (
    MAX_WORKER_ATTEMPTS,
    MAX_WORKER_OUTPUT_CHARS,
    MAX_WORKER_WALL_SECONDS,
    LoopCoordinator,
    WORKER_OUTCOMES,
)


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
    sync = commands.add_parser("sync", help="synchronize registered fact sources")
    sync.add_argument("--force", action="store_true", help="bypass the unchanged-source throttle")
    sync.add_argument("--full", action="store_true", help="rebuild the derived index from fact sources")
    sync.add_argument("--min-interval-seconds", type=int)
    status = commands.add_parser("status", help="return bounded loop health and freshness")
    status.add_argument(
        "--quick",
        action="store_true",
        required=True,
        help="use the bounded fact-source/index non-mutating status contract",
    )
    loop_check = commands.add_parser("loop-check", help="choose one deterministic next loop action")
    loop_check.add_argument(
        "--apply-safe",
        action="store_true",
        help="apply at most one local sync, reindex, or backup action",
    )
    import_plan = commands.add_parser(
        "import-plan",
        help="inspect an allow-listed source set without importing it",
    )
    import_plan.add_argument("project_id")
    import_plan.add_argument("root")
    import_plan.add_argument("--file", action="append", dest="files")
    import_plan.add_argument("--classification", choices=["synthetic", "real"], default="real")
    import_plan.add_argument(
        "--dry-run",
        action="store_true",
        required=True,
        help="required safety acknowledgement; this command never imports",
    )
    approve_import = commands.add_parser(
        "approve-import",
        help="recompute and approve one unchanged real-data import plan",
    )
    approve_import.add_argument("plan_hash")
    approve_import.add_argument("project_id")
    approve_import.add_argument("root")
    approve_import.add_argument("--file", action="append", dest="files")
    approve_import.add_argument("--classification", choices=["real"], default="real")
    approve_import.add_argument("--confirm-approval", action="store_true", required=True)
    evaluate = commands.add_parser("evaluate", help="run a synthetic-only retrieval quality set")
    evaluate.add_argument("cases")
    worker_policy = commands.add_parser(
        "worker-policy",
        help="validate an external worker request against hard protection limits",
    )
    worker_policy.add_argument("max_total_tokens", type=int)
    worker_policy.add_argument("--max-attempts", type=int, default=MAX_WORKER_ATTEMPTS)
    worker_policy.add_argument("--max-output-chars", type=int, default=MAX_WORKER_OUTPUT_CHARS)
    worker_policy.add_argument("--max-wall-seconds", type=int, default=MAX_WORKER_WALL_SECONDS)
    record_worker = commands.add_parser(
        "record-worker",
        help="record privacy-safe external worker outcome metadata",
    )
    record_worker.add_argument("task_id")
    record_worker.add_argument("--max-total-tokens", type=int, required=True)
    record_worker.add_argument("--observed-tokens", type=int, required=True)
    record_worker.add_argument("--attempts", type=int, required=True)
    record_worker.add_argument("--output-chars", type=int, required=True)
    record_worker.add_argument("--wall-seconds", type=float, required=True)
    record_worker.add_argument("--outcome", choices=sorted(WORKER_OUTCOMES), required=True)
    doctor = commands.add_parser("doctor", help="validate fact sources and the derived index")
    doctor.add_argument("--reindex", action="store_true", help="rebuild the derived index before validation")
    commands.add_parser("reindex", help="rebuild SQLite solely from fact sources")
    backup = commands.add_parser("backup", help="archive only authoritative files with hashes")
    backup.add_argument("--destination")
    restore = commands.add_parser("restore", help="verify a backup, restore it, and rebuild the index")
    restore.add_argument("backup")
    restore.add_argument("--destination", required=True)
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "restore":
            result = ContextHub.restore(args.backup, args.destination)
        else:
            hub = ContextHub(args.data_dir)
            loop = LoopCoordinator(hub)
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
            elif args.command == "sync":
                result = loop.sync(
                    force=args.force,
                    full=args.full,
                    min_interval_seconds=args.min_interval_seconds,
                )
            elif args.command == "status":
                result = loop.quick_status()
            elif args.command == "loop-check":
                result = loop.loop_check(apply_safe=args.apply_safe)
            elif args.command == "import-plan":
                result = loop.import_plan(
                    project_id=args.project_id,
                    root=args.root,
                    files=args.files,
                    classification=args.classification,
                )
            elif args.command == "approve-import":
                result = loop.approve_import(
                    args.plan_hash,
                    project_id=args.project_id,
                    root=args.root,
                    files=args.files,
                    classification=args.classification,
                    confirmed=args.confirm_approval,
                )
            elif args.command == "evaluate":
                result = loop.evaluate(args.cases)
            elif args.command == "worker-policy":
                result = loop.worker_policy(
                    max_total_tokens=args.max_total_tokens,
                    max_attempts=args.max_attempts,
                    max_output_chars=args.max_output_chars,
                    max_wall_seconds=args.max_wall_seconds,
                )
            elif args.command == "record-worker":
                result = loop.record_worker(
                    task_id=args.task_id,
                    max_total_tokens=args.max_total_tokens,
                    observed_tokens=args.observed_tokens,
                    attempts=args.attempts,
                    output_chars=args.output_chars,
                    wall_seconds=args.wall_seconds,
                    outcome=args.outcome,
                )
            elif args.command == "doctor":
                result = hub.doctor(reindex=args.reindex)
            elif args.command == "reindex":
                result = hub.reindex()
            elif args.command == "backup":
                result = hub.backup(args.destination)
            else:
                raise AssertionError(f"unhandled command: {args.command}")
        _print_json(result)
        if result.get("ok") is False:
            return 2
        return 0
    except (ContextHubError, FileNotFoundError, PermissionError) as exc:
        _print_json({"ok": False, "error": str(exc), "error_type": type(exc).__name__})
        return 2


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()

"""Deterministic, privacy-safe operating loop for Context Hub."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import tomllib
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .errors import ValidationError
from .hub import (
    DEFAULT_PROJECT_ROOT_FILES,
    ContextHub,
    _atomic_write_text,
    _canonical_json,
    _validate_project_id,
)
from .locking import ExclusiveFileLock


LOOP_STATE_SCHEMA_VERSION = 1
DEFAULT_MIN_SYNC_INTERVAL_SECONDS = 300
DEFAULT_MAX_STALE_SECONDS = 900
DEFAULT_MAX_BACKUP_AGE_SECONDS = 3600
DEFAULT_TELEMETRY_MAX_BYTES = 1024 * 1024
MAX_WORKER_TOKENS = 1_000_000
MAX_WORKER_ATTEMPTS = 3
MAX_WORKER_OUTPUT_CHARS = 65_536
MAX_WORKER_WALL_SECONDS = 3600
WORKER_OUTCOMES = frozenset({"completed", "failed", "token_budget", "wall_time", "cancelled"})
CLASSIFICATIONS = frozenset({"synthetic", "real"})
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _utc_from_epoch(value: float) -> str:
    return (
        datetime.fromtimestamp(value, timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _as_int(value: Any, *, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValidationError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


class LoopCoordinator:
    """Coordinate freshness, recovery, import gates, and local-only evidence."""

    def __init__(self, hub: ContextHub, *, clock: Callable[[], float] = time.time) -> None:
        self.hub = hub
        self.clock = clock
        self.state_path = hub.root / "state" / "loop.json"
        self.telemetry_path = hub.root / "telemetry" / "loop.jsonl"
        self.loop_lock_path = hub.root / "locks" / "loop.lock"
        self.telemetry_lock_path = hub.root / "locks" / "telemetry.lock"

    def _settings(self) -> dict[str, Any]:
        self.hub._require_initialized()
        try:
            with self.hub.config_path.open("rb") as handle:
                config = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ValidationError(f"invalid config.toml: {exc}") from exc
        loop = config.get("loop", {})
        telemetry = config.get("telemetry", {})
        if not isinstance(loop, dict) or not isinstance(telemetry, dict):
            raise ValidationError("loop and telemetry config entries must be TOML tables")
        classification = loop.get("classification", "synthetic")
        if classification not in CLASSIFICATIONS:
            raise ValidationError("loop.classification must be synthetic or real")
        telemetry_enabled = telemetry.get("enabled", False)
        if not isinstance(telemetry_enabled, bool):
            raise ValidationError("telemetry.enabled must be a boolean")
        return {
            "classification": classification,
            "min_sync_interval_seconds": _as_int(
                loop.get("min_sync_interval_seconds", DEFAULT_MIN_SYNC_INTERVAL_SECONDS),
                name="loop.min_sync_interval_seconds",
                minimum=0,
                maximum=86_400,
            ),
            "max_stale_seconds": _as_int(
                loop.get("max_stale_seconds", DEFAULT_MAX_STALE_SECONDS),
                name="loop.max_stale_seconds",
                minimum=1,
                maximum=604_800,
            ),
            "max_backup_age_seconds": _as_int(
                loop.get("max_backup_age_seconds", DEFAULT_MAX_BACKUP_AGE_SECONDS),
                name="loop.max_backup_age_seconds",
                minimum=1,
                maximum=31_536_000,
            ),
            "telemetry_enabled": telemetry_enabled
            or os.environ.get("CONTEXT_HUB_TELEMETRY", "").casefold() in {"1", "true", "yes", "on"},
            "telemetry_max_bytes": _as_int(
                telemetry.get("max_bytes", DEFAULT_TELEMETRY_MAX_BYTES),
                name="telemetry.max_bytes",
                minimum=4096,
                maximum=16 * 1024 * 1024,
            ),
        }

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        return {
            "schema_version": LOOP_STATE_SCHEMA_VERSION,
            "last_sync_at": None,
            "last_sync_epoch": None,
            "authority_fingerprint": None,
            "import_gate": None,
            "worker": None,
        }

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return self._empty_state()
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError(f"invalid loop state: {exc}") from exc
        if not isinstance(value, dict) or value.get("schema_version") != LOOP_STATE_SCHEMA_VERSION:
            raise ValidationError("unsupported or malformed loop state")
        state = self._empty_state()
        state.update(value)
        return state

    def _write_state(self, state: dict[str, Any]) -> None:
        allowed = {
            "schema_version",
            "last_sync_at",
            "last_sync_epoch",
            "authority_fingerprint",
            "import_gate",
            "worker",
        }
        sanitized = {key: state.get(key) for key in allowed}
        sanitized["schema_version"] = LOOP_STATE_SCHEMA_VERSION
        _atomic_write_text(
            self.state_path,
            json.dumps(sanitized, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )

    def _authority_snapshot(self) -> dict[str, Any]:
        self.hub._require_initialized()
        manifest = self.hub._load_manifest()
        entries: list[dict[str, Any]] = []
        missing = 0
        for name, path in (
            ("config", self.hub.config_path),
            ("manifest", self.hub.manifest_path),
            ("events", self.hub.events_path),
        ):
            stat = path.stat()
            entries.append(
                {
                    "type": name,
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "ctime_ns": stat.st_ctime_ns,
                }
            )
        project_files = 0
        for project_id in sorted(manifest["projects"]):
            project = manifest["projects"][project_id]
            root = Path(project["root"])
            for relative_path in sorted(project.get("files", []), key=str.casefold):
                project_files += 1
                try:
                    path = self.hub._resolve_project_file(root, relative_path, require_exists=True)
                    stat = path.stat()
                    entry = {
                        "type": "project",
                        "project_id": project_id,
                        "relative_path": relative_path,
                        "size": stat.st_size,
                        "mtime_ns": stat.st_mtime_ns,
                        "ctime_ns": stat.st_ctime_ns,
                    }
                except FileNotFoundError:
                    missing += 1
                    entry = {
                        "type": "project",
                        "project_id": project_id,
                        "relative_path": relative_path,
                        "missing": True,
                    }
                entries.append(entry)
        fingerprint = hashlib.sha256(_canonical_json(entries).encode("utf-8")).hexdigest()
        return {
            "fingerprint": fingerprint,
            "project_count": len(manifest["projects"]),
            "project_file_count": project_files,
            "missing_project_files": missing,
        }

    def sync(
        self,
        *,
        force: bool = False,
        full: bool = False,
        min_interval_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Synchronize changed sources, throttling only when facts are unchanged."""
        started = self.clock()
        settings = self._settings()
        interval = settings["min_sync_interval_seconds"] if min_interval_seconds is None else _as_int(
            min_interval_seconds,
            name="min_interval_seconds",
            minimum=0,
            maximum=86_400,
        )
        with ExclusiveFileLock(self.loop_lock_path):
            before = self._authority_snapshot()
            state = self._load_state()
            last_epoch = state.get("last_sync_epoch")
            age = started - last_epoch if isinstance(last_epoch, (int, float)) else None
            if (
                not force
                and not full
                and state.get("authority_fingerprint") == before["fingerprint"]
                and age is not None
                and 0 <= age < interval
            ):
                result = {
                    "ok": True,
                    "action": "throttled",
                    "classification": settings["classification"],
                    "freshness": "current",
                    "changed": False,
                    "age_seconds": round(age, 3),
                    "project_count": before["project_count"],
                    "project_file_count": before["project_file_count"],
                    "missing_project_files": before["missing_project_files"],
                }
            else:
                if full:
                    sync_result = self.hub.reindex()
                    action = "reindexed"
                else:
                    with ExclusiveFileLock(self.hub.lock_path):
                        sync_result = self.hub._sync_registered_sources_locked(
                            self.hub._load_manifest()
                        )
                    action = "synchronized"
                finished = self.clock()
                after = self._authority_snapshot()
                state["last_sync_epoch"] = finished
                state["last_sync_at"] = _utc_from_epoch(finished)
                state["authority_fingerprint"] = after["fingerprint"]
                self._write_state(state)
                changed_count = int(sync_result.get("indexed_sections", 0)) + int(
                    sync_result.get("removed_sections", 0)
                )
                if full:
                    changed_count = int(sync_result.get("sections", 0))
                result = {
                    "ok": True,
                    "action": action,
                    "classification": settings["classification"],
                    "freshness": "current",
                    "changed": bool(changed_count),
                    "indexed_sections": int(
                        sync_result.get("indexed_sections", sync_result.get("sections", 0))
                    ),
                    "removed_sections": int(sync_result.get("removed_sections", 0)),
                    "project_count": after["project_count"],
                    "project_file_count": after["project_file_count"],
                    "missing_project_files": after["missing_project_files"],
                }
        self._emit("sync", "ok", duration_ms=(self.clock() - started) * 1000)
        return result

    def quick_status(self) -> dict[str, Any]:
        """Return bounded health and freshness without parsing the full JSONL."""
        started = self.clock()
        settings = self._settings()
        snapshot = self._authority_snapshot()
        state = self._load_state()
        now = self.clock()
        last_epoch = state.get("last_sync_epoch")
        age = now - last_epoch if isinstance(last_epoch, (int, float)) else None
        if state.get("authority_fingerprint") != snapshot["fingerprint"]:
            freshness = "stale"
        elif age is None:
            freshness = "unknown"
        elif age > settings["max_stale_seconds"]:
            freshness = "stale"
        else:
            freshness = "current"

        index_ok = self.hub.index_path.is_file()
        counts = {"events": 0, "sections": 0}
        try:
            if not index_ok:
                raise sqlite3.DatabaseError("derived index is missing")
            index_uri = f"{self.hub.index_path.resolve().as_uri()}?mode=ro"
            with closing(sqlite3.connect(index_uri, uri=True, timeout=5)) as connection:
                connection.execute("PRAGMA query_only=ON")
                counts = {
                    "events": int(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]),
                    "sections": int(connection.execute("SELECT COUNT(*) FROM sections").fetchone()[0]),
                }
        except (OSError, sqlite3.DatabaseError):
            index_ok = False

        backup_age: float | None = None
        if self.hub.backups_dir.exists():
            candidates = [path for path in self.hub.backups_dir.glob("*.zip") if path.is_file()]
            if candidates:
                backup_age = max(0.0, now - max(path.stat().st_mtime for path in candidates))

        gate = state.get("import_gate") if isinstance(state.get("import_gate"), dict) else None
        worker = state.get("worker") if isinstance(state.get("worker"), dict) else None
        result = {
            "ok": index_ok and snapshot["missing_project_files"] == 0,
            "classification": settings["classification"],
            "freshness": freshness,
            "last_sync_at": state.get("last_sync_at"),
            "sync_age_seconds": None if age is None else round(max(0.0, age), 3),
            "index_ok": index_ok,
            "counts": counts,
            "project_count": snapshot["project_count"],
            "project_file_count": snapshot["project_file_count"],
            "missing_project_files": snapshot["missing_project_files"],
            "backup": {
                "present": backup_age is not None,
                "age_seconds": None if backup_age is None else round(backup_age, 3),
                "fresh": backup_age is not None
                and backup_age <= settings["max_backup_age_seconds"],
            },
            "import_gate": None
            if gate is None
            else {
                "plan_hash": gate.get("plan_hash"),
                "project_id": gate.get("project_id"),
                "classification": gate.get("classification"),
                "candidate_count": gate.get("candidate_count"),
                "approved": gate.get("approved", False),
            },
            "worker": worker,
        }
        self._emit("status", "ok" if result["ok"] else "failed", duration_ms=(self.clock() - started) * 1000)
        return result

    def _choose_action(
        self,
        status: dict[str, Any],
        settings: dict[str, Any],
    ) -> tuple[str, str]:
        if not status["index_ok"]:
            return "repair_index", "derived index is unavailable"
        elif status["missing_project_files"]:
            return "sync", "registered sources are missing"
        elif status["freshness"] != "current":
            return "sync", "authority fingerprint or freshness window changed"
        elif settings["classification"] == "real" and not (
            status["import_gate"] and status["import_gate"]["approved"]
        ):
            return "review_import", "real-data import gate is closed"
        elif isinstance(status["worker"], dict) and (
            status["worker"].get("outcome") in {"failed", "token_budget", "wall_time"}
            or status["worker"].get("needs_review") is True
        ):
            return "inspect_worker", "last external worker did not complete normally"
        elif not status["backup"]["fresh"]:
            return "backup", "no backup exists inside the configured freshness window"
        return "ready", "all loop gates are satisfied"

    def loop_check(self, *, apply_safe: bool = False) -> dict[str, Any]:
        """Choose one deterministic next action and optionally apply one safe action."""
        status = self.quick_status()
        settings = self._settings()
        action, reason = self._choose_action(status, settings)

        applied = False
        evidence: dict[str, Any] | None = None
        if apply_safe and action in {"repair_index", "sync", "backup"}:
            if action == "repair_index":
                evidence = self.sync(force=True, full=True)
            elif action == "sync":
                evidence = self.sync(force=True)
            else:
                backup = self.hub.backup()
                evidence = {
                    "ok": bool(backup.get("ok")),
                    "sha256": backup.get("sha256"),
                    "entries": backup.get("entries", []),
                }
            applied = True
        final_status = self.quick_status() if applied else status
        next_action, next_reason = self._choose_action(final_status, settings)
        return {
            "ok": bool(evidence.get("ok", False)) if applied and evidence else True,
            "action": action,
            "reason": reason,
            "applied": applied,
            "evidence": evidence,
            "next_action": next_action,
            "next_reason": next_reason,
            "ready": next_action == "ready",
            "status": final_status,
        }

    def import_plan(
        self,
        *,
        project_id: str,
        root: str | os.PathLike[str],
        files: list[str] | None = None,
        classification: str = "real",
    ) -> dict[str, Any]:
        """Inspect an allow-listed source set without modifying hub or loop state."""
        _validate_project_id(project_id)
        if classification not in CLASSIFICATIONS:
            raise ValidationError("classification must be synthetic or real")
        resolved_root = Path(root).expanduser().resolve(strict=True)
        if not resolved_root.is_dir():
            raise ValidationError("import root must be a directory")
        requested: Iterable[str]
        explicit = files is not None
        if explicit:
            if not isinstance(files, list):
                raise ValidationError("files must be a list when supplied")
            requested = files
        else:
            defaults = list(DEFAULT_PROJECT_ROOT_FILES)
            context_dir = resolved_root / "context"
            if context_dir.is_dir():
                defaults.extend(
                    f"context/{path.name}"
                    for path in sorted(context_dir.iterdir(), key=lambda item: item.name.casefold())
                    if path.is_file() and path.suffix.casefold() == ".md"
                )
            requested = defaults

        candidates: list[dict[str, Any]] = []
        rejected: list[dict[str, str]] = []
        seen: set[str] = set()
        for raw in requested:
            try:
                relative = self.hub._normalize_relative_file(raw)
                if relative.casefold() in seen:
                    continue
                path = self.hub._resolve_project_file(resolved_root, relative, require_exists=True)
                if not path.is_file():
                    raise ValidationError("source is not a regular file")
                before = path.stat()
                raw_bytes = path.read_bytes()
                after = path.stat()
                before_signature = (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                after_signature = (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                if before_signature != after_signature:
                    raise ValidationError("source changed while building import plan")
                raw_bytes.decode("utf-8")
                candidates.append(
                    {
                        "relative_path": relative,
                        "bytes": after.st_size,
                        "mtime_ns": after.st_mtime_ns,
                        "ctime_ns": after.st_ctime_ns,
                        "sha256": hashlib.sha256(raw_bytes).hexdigest(),
                    }
                )
                seen.add(relative.casefold())
            except (FileNotFoundError, OSError, UnicodeDecodeError, ValidationError) as exc:
                if explicit:
                    raw_text = str(raw)
                    try:
                        display_entry = self.hub._normalize_relative_file(raw_text)
                    except ValidationError:
                        display_entry = "[invalid-entry]"
                    rejected.append({"entry": display_entry, "reason": type(exc).__name__})

        # Bind approval to the exact normalized root without returning or persisting
        # that potentially private path. Identical files under another root must be
        # reviewed as a different import plan.
        root_fingerprint = hashlib.sha256(
            os.fsencode(os.path.normcase(str(resolved_root)))
        ).hexdigest()
        canonical = {
            "schema_version": 1,
            "project_id": project_id,
            "classification": classification,
            "root_fingerprint": root_fingerprint,
            "candidates": candidates,
            "rejected": rejected,
        }
        plan_hash = hashlib.sha256(_canonical_json(canonical).encode("utf-8")).hexdigest()
        return {
            "ok": not rejected and bool(candidates),
            "dry_run": True,
            "mutated": False,
            "project_id": project_id,
            "classification": classification,
            "candidate_count": len(candidates),
            "rejected_count": len(rejected),
            "candidates": candidates,
            "rejected": rejected,
            "plan_hash": plan_hash,
            "approval_required": classification == "real",
            "approved": self.is_import_approved(plan_hash),
        }

    def approve_import(
        self,
        plan_hash: str,
        *,
        project_id: str,
        root: str | os.PathLike[str],
        files: list[str] | None = None,
        classification: str = "real",
        confirmed: bool,
    ) -> dict[str, Any]:
        """Approve only a plan that still matches the current allow-listed files."""
        if not confirmed:
            raise ValidationError("import approval requires explicit confirmation")
        if re.fullmatch(r"[0-9a-f]{64}", plan_hash) is None:
            raise ValidationError("plan_hash must be a lowercase SHA-256 digest")
        if classification != "real":
            raise ValidationError("only real-data import plans require explicit approval")
        current = self.import_plan(
            project_id=project_id,
            root=root,
            files=files,
            classification=classification,
        )
        if not current["ok"]:
            raise ValidationError("current import plan contains rejected or missing sources")
        if current["plan_hash"] != plan_hash:
            raise ValidationError("import plan changed; run import-plan --dry-run again")
        now = self.clock()
        with ExclusiveFileLock(self.loop_lock_path):
            state = self._load_state()
            state["import_gate"] = {
                "plan_hash": plan_hash,
                "project_id": project_id,
                "classification": classification,
                "candidate_count": current["candidate_count"],
                "approved": True,
                "approved_at": _utc_from_epoch(now),
            }
            self._write_state(state)
        self._emit("import_gate", "approved")
        return {
            "ok": True,
            "plan_hash": plan_hash,
            "project_id": project_id,
            "classification": classification,
            "candidate_count": current["candidate_count"],
            "approved": True,
        }

    def is_import_approved(self, plan_hash: str) -> bool:
        state = self._load_state()
        gate = state.get("import_gate")
        return bool(
            isinstance(gate, dict)
            and gate.get("approved") is True
            and gate.get("plan_hash") == plan_hash
        )

    def worker_policy(
        self,
        *,
        max_total_tokens: int,
        max_attempts: int = MAX_WORKER_ATTEMPTS,
        max_output_chars: int = MAX_WORKER_OUTPUT_CHARS,
        max_wall_seconds: int = MAX_WORKER_WALL_SECONDS,
    ) -> dict[str, Any]:
        accepted = (
            isinstance(max_total_tokens, int)
            and not isinstance(max_total_tokens, bool)
            and 1 <= max_total_tokens <= MAX_WORKER_TOKENS
            and isinstance(max_attempts, int)
            and not isinstance(max_attempts, bool)
            and 1 <= max_attempts <= MAX_WORKER_ATTEMPTS
            and isinstance(max_output_chars, int)
            and not isinstance(max_output_chars, bool)
            and 1 <= max_output_chars <= MAX_WORKER_OUTPUT_CHARS
            and isinstance(max_wall_seconds, int)
            and not isinstance(max_wall_seconds, bool)
            and 1 <= max_wall_seconds <= MAX_WORKER_WALL_SECONDS
        )
        return {
            "ok": accepted,
            "accepted": accepted,
            "limits": {
                "max_total_tokens": MAX_WORKER_TOKENS,
                "max_attempts": MAX_WORKER_ATTEMPTS,
                "max_output_chars": MAX_WORKER_OUTPUT_CHARS,
                "max_wall_seconds": MAX_WORKER_WALL_SECONDS,
            },
            "requested": {
                "max_total_tokens": max_total_tokens,
                "max_attempts": max_attempts,
                "max_output_chars": max_output_chars,
                "max_wall_seconds": max_wall_seconds,
            },
            "usage_accounting": "provider_reported_after_response",
        }

    def record_worker(
        self,
        *,
        task_id: str,
        max_total_tokens: int,
        observed_tokens: int,
        attempts: int,
        output_chars: int,
        wall_seconds: float,
        outcome: str,
    ) -> dict[str, Any]:
        if _SAFE_IDENTIFIER_RE.fullmatch(task_id) is None:
            raise ValidationError("task_id must be a non-sensitive identifier")
        policy = self.worker_policy(max_total_tokens=max_total_tokens)
        if not policy["accepted"]:
            raise ValidationError("worker request exceeds the supported protection policy")
        observed_tokens = _as_int(
            observed_tokens, name="observed_tokens", minimum=0, maximum=100_000_000
        )
        attempts = _as_int(attempts, name="attempts", minimum=1, maximum=MAX_WORKER_ATTEMPTS)
        output_chars = _as_int(
            output_chars, name="output_chars", minimum=0, maximum=100_000_000
        )
        if not isinstance(wall_seconds, (int, float)) or isinstance(wall_seconds, bool) or wall_seconds < 0:
            raise ValidationError("wall_seconds must be a non-negative number")
        if outcome not in WORKER_OUTCOMES:
            raise ValidationError("unsupported worker outcome")
        token_overshoot = observed_tokens > max_total_tokens
        output_truncated = output_chars > MAX_WORKER_OUTPUT_CHARS
        wall_time_exceeded = float(wall_seconds) > MAX_WORKER_WALL_SECONDS
        needs_review = (
            outcome in {"failed", "token_budget", "wall_time"}
            or token_overshoot
            or output_truncated
            or wall_time_exceeded
        )
        record = {
            "task_id_hash": hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:16],
            "budget_tokens": max_total_tokens,
            "observed_tokens": observed_tokens,
            "attempts": attempts,
            "output_chars": min(output_chars, MAX_WORKER_OUTPUT_CHARS),
            "output_truncated": output_truncated,
            "wall_seconds": round(float(wall_seconds), 3),
            "outcome": outcome,
            "token_overshoot": token_overshoot,
            "wall_time_exceeded": wall_time_exceeded,
            "protection_exceeded": token_overshoot or wall_time_exceeded,
            "needs_review": needs_review,
            "recorded_at": _utc_from_epoch(self.clock()),
        }
        with ExclusiveFileLock(self.loop_lock_path):
            state = self._load_state()
            state["worker"] = record
            self._write_state(state)
        self._emit("worker", outcome, counts={"attempts": attempts})
        return {"ok": True, "worker": record, "policy": policy["limits"]}

    def evaluate(self, cases_path: str | os.PathLike[str]) -> dict[str, Any]:
        if self._settings()["classification"] != "synthetic":
            raise ValidationError("evaluation is allowed only for a synthetic-classified data root")
        try:
            value = json.loads(Path(cases_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError(f"invalid evaluation cases: {exc}") from exc
        if not isinstance(value, dict) or value.get("synthetic_only") is not True:
            raise ValidationError("evaluation cases must declare synthetic_only=true")
        cases = value.get("cases")
        if not isinstance(cases, list) or not cases:
            raise ValidationError("evaluation cases must contain a non-empty cases array")
        sync_result = self.sync()
        failed: list[str] = []
        top1 = 0
        top3 = 0
        for index, case in enumerate(cases):
            if not isinstance(case, dict):
                raise ValidationError("each evaluation case must be an object")
            case_id = case.get("id")
            query = case.get("query")
            expected = case.get("expected_project_id")
            if (
                not isinstance(case_id, str)
                or _SAFE_IDENTIFIER_RE.fullmatch(case_id) is None
                or not isinstance(query, str)
                or not query
                or not isinstance(expected, str)
            ):
                raise ValidationError(f"invalid evaluation case at index {index}")
            result = self.hub.search(query, limit=3, max_chars=200)
            project_ids = [item.get("project_id") for item in result.get("items", [])]
            top1 += int(bool(project_ids) and project_ids[0] == expected)
            top3 += int(expected in project_ids)
            if expected not in project_ids:
                failed.append(case_id)
        total = len(cases)
        result = {
            "ok": not failed,
            "synthetic_only": True,
            "freshness": sync_result["freshness"],
            "cases": total,
            "top1_accuracy": round(top1 / total, 6),
            "top3_accuracy": round(top3 / total, 6),
            "failed_case_ids": failed,
        }
        self._emit("evaluate", "ok" if result["ok"] else "failed", counts={"cases": total})
        return result

    def _emit(
        self,
        operation: str,
        outcome: str,
        *,
        duration_ms: float | None = None,
        counts: dict[str, int] | None = None,
    ) -> None:
        settings = self._settings()
        if not settings["telemetry_enabled"]:
            return
        event: dict[str, Any] = {
            "schema_version": 1,
            "at": _utc_from_epoch(self.clock()),
            "operation": operation,
            "outcome": outcome,
            "classification": settings["classification"],
        }
        if duration_ms is not None:
            event["duration_ms"] = round(max(0.0, float(duration_ms)), 3)
        if counts:
            event["counts"] = {
                key: int(value)
                for key, value in counts.items()
                if isinstance(key, str) and isinstance(value, int) and not isinstance(value, bool)
            }
        line = (_canonical_json(event) + "\n").encode("utf-8")
        max_bytes = settings["telemetry_max_bytes"]
        with ExclusiveFileLock(self.telemetry_lock_path):
            existing = self.telemetry_path.read_bytes() if self.telemetry_path.exists() else b""
            if len(existing) + len(line) > max_bytes:
                keep = max(0, max_bytes - len(line))
                existing = existing[-keep:] if keep else b""
                newline = existing.find(b"\n")
                if newline >= 0:
                    existing = existing[newline + 1 :]
            payload = (existing + line)[-max_bytes:]
            _atomic_write_text(self.telemetry_path, payload.decode("utf-8", errors="ignore"))

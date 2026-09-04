"""Authoritative JSONL/Markdown store and rebuildable SQLite index."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import tomllib
import uuid
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .errors import IndexPendingError, NotFoundError, ValidationError
from .locking import ExclusiveFileLock


SCHEMA_VERSION = 1
ALLOWED_ACTIONS = frozenset({"append", "supersede", "tombstone"})
ALLOWED_KINDS = frozenset({"preference", "decision", "constraint", "fact", "status"})
DEFAULT_PROJECT_ROOT_FILES = ("AGENTS.md",)
ALLOWED_TEXT_SUFFIXES = frozenset(
    {
        ".md",
        ".txt",
        ".rst",
        ".toml",
        ".yaml",
        ".yml",
        ".json",
        ".jsonl",
        ".csv",
        ".tsv",
    }
)
EXCLUDED_PATH_PARTS = frozenset(
    {
        ".git",
        ".cache",
        "node_modules",
        "backup",
        "backups",
        "browser data",
        "user data",
    }
)
EXCLUDED_FILE_SUFFIXES = frozenset({".key", ".pem", ".pfx", ".p12", ".crt", ".cer"})
PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
EVENT_ID_RE = re.compile(
    r"^evt_([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$"
)
HEADING_RE = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t]*(?:\r?\n)?$")


def default_data_dir() -> Path:
    override = os.environ.get("CONTEXT_HUB_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return (Path(local_app_data) / "ContextHub").resolve()
    return (Path.home() / ".local" / "share" / "ContextHub").resolve()


def sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _bounded_int(value: int, *, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValidationError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


def _validate_project_id(project_id: str | None) -> str | None:
    if project_id is not None and (
        not isinstance(project_id, str) or PROJECT_ID_RE.fullmatch(project_id) is None
    ):
        raise ValidationError("project_id must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}")
    return project_id


def _normalize_event_id(event_id: str) -> str:
    if not isinstance(event_id, str):
        raise ValidationError("event_id must use evt_<uuid4> format")
    match = EVENT_ID_RE.fullmatch(event_id.casefold())
    if match is None:
        raise ValidationError("event_id must use evt_<uuid4> format")
    parsed = uuid.UUID(match.group(1))
    if parsed.version != 4:
        raise ValidationError("event_id must contain a UUID4")
    return f"evt_{parsed}"


def _encode_cursor(payload: dict[str, Any]) -> str:
    raw = _canonical_json(payload).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> dict[str, Any]:
    if not isinstance(cursor, str) or not cursor:
        raise ValidationError("invalid read cursor")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("invalid read cursor") from exc
    if not isinstance(value, dict) or value.get("v") != 1:
        raise ValidationError("unsupported read cursor")
    return value


class ContextHub:
    """One local Context Hub data root."""

    def __init__(self, data_dir: str | os.PathLike[str] | None = None):
        self.root = Path(data_dir).expanduser().resolve() if data_dir else default_data_dir()
        self.config_path = self.root / "config.toml"
        self.manifest_path = self.root / "manifest.json"
        self.events_path = self.root / "memory" / "events.jsonl"
        self.index_path = self.root / "index" / "context.sqlite3"
        self.lock_path = self.root / "locks" / "events.lock"
        self.backups_dir = self.root / "backups"

    def initialize(self, *, write_enabled: bool = False) -> dict[str, Any]:
        for directory in (
            self.root,
            self.events_path.parent,
            self.index_path.parent,
            self.lock_path.parent,
            self.backups_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        with ExclusiveFileLock(self.lock_path):
            if not self.config_path.exists():
                config = f"schema_version = {SCHEMA_VERSION}\nwrite_enabled = {str(write_enabled).lower()}\n"
                _atomic_write_text(self.config_path, config)
            elif write_enabled and not self.write_enabled:
                config_text = self.config_path.read_text(encoding="utf-8")
                updated_config, replacements = re.subn(
                    r"(?m)^write_enabled\s*=\s*(?:true|false)\s*$",
                    "write_enabled = true",
                    config_text,
                )
                if replacements != 1:
                    raise ValidationError("config.toml must contain one boolean write_enabled setting")
                _atomic_write_text(self.config_path, updated_config)
            if not self.manifest_path.exists():
                self._write_manifest(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "updated_at": utc_now(),
                        "projects": {},
                    }
                )
            if not self.events_path.exists():
                _atomic_write_text(self.events_path, "")

            manifest = self._load_manifest()
            if not self.index_path.exists():
                self._reindex_locked(manifest)
            else:
                try:
                    with closing(self._connect()) as connection:
                        self._create_schema(connection)
                    self._sync_registered_sources_locked(manifest)
                except sqlite3.DatabaseError as exc:
                    raise IndexPendingError("derived index is unreadable; run reindex") from exc
        return {
            "ok": True,
            "data_dir": str(self.root),
            "write_enabled": self.write_enabled,
            "schema_version": SCHEMA_VERSION,
        }

    @property
    def write_enabled(self) -> bool:
        if not self.config_path.exists():
            return False
        try:
            with self.config_path.open("rb") as handle:
                config = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ValidationError(f"invalid config.toml: {exc}") from exc
        if config.get("schema_version") != SCHEMA_VERSION:
            raise ValidationError("unsupported config.toml schema_version")
        value = config.get("write_enabled", False)
        if not isinstance(value, bool):
            raise ValidationError("config.toml write_enabled must be boolean")
        return value

    def _require_initialized(self) -> None:
        required = (self.config_path, self.manifest_path, self.events_path)
        if any(not path.exists() for path in required):
            raise ValidationError("Context Hub is not initialized; run init first")

    def _load_manifest(self) -> dict[str, Any]:
        self._require_initialized()
        try:
            value = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError(f"invalid manifest.json: {exc}") from exc
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != SCHEMA_VERSION
            or not isinstance(value.get("updated_at"), str)
            or not isinstance(value.get("projects"), dict)
        ):
            raise ValidationError("unsupported or malformed manifest.json")
        for project_id, project in value["projects"].items():
            try:
                _validate_project_id(project_id)
                if not isinstance(project, dict):
                    raise ValidationError("project entry must be an object")
                root = project.get("root")
                if not isinstance(root, str) or not Path(root).is_absolute():
                    raise ValidationError("project root must be an absolute path")
                files = project.get("files")
                if not isinstance(files, list):
                    raise ValidationError("project files must be a list")
                seen: set[str] = set()
                for raw_path in files:
                    normalized = self._normalize_relative_file(raw_path)
                    if normalized != raw_path:
                        raise ValidationError("project file path must be canonical")
                    folded = normalized.casefold()
                    if folded in seen:
                        raise ValidationError("project files contain a duplicate path")
                    seen.add(folded)
            except ValidationError as exc:
                raise ValidationError(f"malformed manifest project {project_id!r}: {exc}") from exc
        return value

    def _write_manifest(self, manifest: dict[str, Any]) -> None:
        _atomic_write_text(self.manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

    def _connect(self, path: Path | None = None, *, wal: bool = True) -> sqlite3.Connection:
        database_path = path or self.index_path
        connection = sqlite3.connect(database_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        # JSONL is the fsynced authority. The SQLite index is rebuildable, so
        # WAL + NORMAL avoids a second full disk flush for every event without
        # weakening the authoritative write boundary.
        connection.execute("PRAGMA synchronous=NORMAL")
        if wal:
            connection.execute("PRAGMA journal_mode=WAL")
        return connection

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                line_no INTEGER NOT NULL UNIQUE,
                action TEXT NOT NULL,
                kind TEXT NOT NULL,
                project_id TEXT,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_ref TEXT,
                supersedes TEXT,
                tombstone INTEGER NOT NULL,
                content_sha256 TEXT NOT NULL,
                active INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_project_kind_active
                ON events(project_id, kind, active);
            CREATE VIRTUAL TABLE IF NOT EXISTS event_fts USING fts5(
                content,
                event_id UNINDEXED,
                tokenize='trigram'
            );
            CREATE TABLE IF NOT EXISTS sections (
                ref TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                section_index INTEGER NOT NULL,
                heading TEXT,
                content TEXT NOT NULL,
                line_start INTEGER NOT NULL,
                char_start INTEGER NOT NULL,
                char_end INTEGER NOT NULL,
                modified_at TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                file_sha256 TEXT NOT NULL,
                source_ref TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS sections_project_path
                ON sections(project_id, relative_path);
            CREATE VIRTUAL TABLE IF NOT EXISTS section_fts USING fts5(
                content,
                ref UNINDEXED,
                tokenize='trigram'
            );
            """
        )
        # Fresh data roots use the complete schema above. These additive columns
        # keep pre-release test roots rebuildable without making SQLite authoritative.
        existing_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(sections)").fetchall()
        }
        migrations = {
            "line_start": "INTEGER NOT NULL DEFAULT 1",
            "char_start": "INTEGER NOT NULL DEFAULT 0",
            "char_end": "INTEGER NOT NULL DEFAULT 0",
            "modified_at": "TEXT NOT NULL DEFAULT ''",
        }
        for column, declaration in migrations.items():
            if column not in existing_columns:
                connection.execute(f"ALTER TABLE sections ADD COLUMN {column} {declaration}")
        connection.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        connection.commit()

    def _read_events(self) -> list[dict[str, Any]]:
        self._require_initialized()
        events: list[dict[str, Any]] = []
        seen: set[str] = set()
        with self.events_path.open("rb") as handle:
            for line_no, raw_line in enumerate(handle, start=1):
                if not raw_line.strip():
                    continue
                try:
                    event = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValidationError(f"invalid JSONL at line {line_no}: {exc}") from exc
                self._validate_stored_event(event, line_no)
                event_id = event["event_id"]
                if event_id in seen:
                    raise ValidationError(f"duplicate event_id at line {line_no}: {event_id}")
                seen.add(event_id)
                events.append(event)
        return events

    @staticmethod
    def _validate_stored_event(event: Any, line_no: int) -> None:
        if not isinstance(event, dict):
            raise ValidationError(f"event at line {line_no} is not an object")
        required = {
            "schema_version",
            "event_id",
            "action",
            "kind",
            "project_id",
            "content",
            "created_at",
            "source",
            "supersedes",
            "tombstone",
            "content_sha256",
        }
        missing = required.difference(event)
        if missing:
            raise ValidationError(f"event at line {line_no} is missing {sorted(missing)}")
        if event["schema_version"] != SCHEMA_VERSION:
            raise ValidationError(f"event at line {line_no} has unsupported schema_version")
        try:
            canonical_event_id = _normalize_event_id(event["event_id"])
        except ValidationError as exc:
            raise ValidationError(f"event at line {line_no} has an invalid event_id") from exc
        if canonical_event_id != event["event_id"]:
            raise ValidationError(f"event at line {line_no} has a non-canonical event_id")
        action = event["action"]
        kind = event["kind"]
        if (
            not isinstance(action, str)
            or action not in ALLOWED_ACTIONS
            or not isinstance(kind, str)
            or kind not in ALLOWED_KINDS
        ):
            raise ValidationError(f"event at line {line_no} has an invalid action or kind")
        try:
            _validate_project_id(event["project_id"])
        except ValidationError as exc:
            raise ValidationError(f"event at line {line_no} has an invalid project_id") from exc
        if not isinstance(event["content"], str) or len(event["content"]) > 4000:
            raise ValidationError(f"event at line {line_no} has invalid content")
        if event["action"] in {"append", "supersede"} and not event["content"]:
            raise ValidationError(f"event at line {line_no} requires non-empty content")
        if event["action"] == "tombstone" and event["content"]:
            raise ValidationError(f"event at line {line_no} tombstone content must be empty")
        if sha256_text(event["content"]) != event["content_sha256"]:
            raise ValidationError(f"event at line {line_no} has a content hash mismatch")
        created_at = event["created_at"]
        if not isinstance(created_at, str):
            raise ValidationError(f"event at line {line_no} has an invalid created_at")
        try:
            parsed_created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(f"event at line {line_no} has an invalid created_at") from exc
        if parsed_created_at.tzinfo is None:
            raise ValidationError(f"event at line {line_no} created_at must include a timezone")
        if not isinstance(event["source"], dict) or not isinstance(event["source"].get("type"), str):
            raise ValidationError(f"event at line {line_no} has an invalid source")
        source_type = event["source"].get("type")
        source_ref = event["source"].get("ref")
        if not source_type or len(source_type) > 64 or not isinstance(source_ref, str) or not source_ref:
            raise ValidationError(f"event at line {line_no} has an invalid source")
        if len(source_ref) > 1000:
            raise ValidationError(f"event at line {line_no} source ref is too long")
        expected_tombstone = event["action"] == "tombstone"
        if not isinstance(event["tombstone"], bool) or event["tombstone"] is not expected_tombstone:
            raise ValidationError(f"event at line {line_no} has inconsistent tombstone state")
        target = event["supersedes"]
        if event["action"] == "append" and target is not None:
            raise ValidationError(f"event at line {line_no} append cannot supersede")
        if event["action"] in {"supersede", "tombstone"}:
            try:
                normalized_target = _normalize_event_id(target)
            except ValidationError as exc:
                raise ValidationError(f"event at line {line_no} has invalid supersedes") from exc
            if normalized_target != target:
                raise ValidationError(f"event at line {line_no} has non-canonical supersedes")

    @staticmethod
    def _active_event_ids(events: Iterable[dict[str, Any]]) -> set[str]:
        active: set[str] = set()
        for event in events:
            target = event.get("supersedes")
            if target:
                active.discard(target)
            if not event.get("tombstone"):
                active.add(event["event_id"])
        return active

    def put(
        self,
        *,
        action: str,
        kind: str,
        content: str,
        project_id: str | None = None,
        source_type: str = "manual",
        source_ref: str | None = None,
        supersedes: str | None = None,
        event_id: str | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        self._require_initialized()
        if not confirmed:
            raise ValidationError("write requires explicit confirmation")
        if not isinstance(action, str) or action not in ALLOWED_ACTIONS:
            raise ValidationError(f"action must be one of {sorted(ALLOWED_ACTIONS)}")
        if not isinstance(kind, str) or kind not in ALLOWED_KINDS:
            raise ValidationError(f"kind must be one of {sorted(ALLOWED_KINDS)}")
        if not isinstance(content, str):
            raise ValidationError("content must be a string")
        if len(content) > 4000:
            raise ValidationError("content exceeds the 4000 character MVP limit")
        if action in {"append", "supersede"} and not content:
            raise ValidationError(f"{action} requires non-empty content")
        if action == "tombstone" and content:
            raise ValidationError("tombstone content must be empty")
        project_id = _validate_project_id(project_id)
        if not isinstance(source_type, str) or not source_type or len(source_type) > 64:
            raise ValidationError("source_type must contain 1 to 64 characters")
        if not isinstance(source_ref, str) or not source_ref.strip():
            raise ValidationError("source_ref is required")
        if len(source_ref) > 1000:
            raise ValidationError("source_ref exceeds 1000 characters")
        if action == "append" and supersedes is not None:
            raise ValidationError("append cannot specify supersedes")
        if action in {"supersede", "tombstone"} and not supersedes:
            raise ValidationError(f"{action} requires supersedes")
        if event_id is not None:
            event_id = _normalize_event_id(event_id)
        else:
            event_id = f"evt_{uuid.uuid4()}"
        if supersedes is not None:
            supersedes = _normalize_event_id(supersedes)

        request_identity = {
            "action": action,
            "kind": kind,
            "project_id": project_id,
            "content": content,
            "source": {"type": source_type, "ref": source_ref},
            "supersedes": supersedes,
            "tombstone": action == "tombstone",
            "content_sha256": sha256_text(content),
        }

        with ExclusiveFileLock(self.lock_path):
            events = self._read_events()
            manifest = self._load_manifest()
            if project_id is not None and project_id not in manifest["projects"]:
                raise ValidationError("project_id is not registered")
            by_id = {event["event_id"]: event for event in events}
            if event_id in by_id:
                existing = by_id[event_id]
                if all(existing.get(key) == value for key, value in request_identity.items()):
                    return {
                        "status": "existing",
                        "event_id": event_id,
                        "content_sha256": existing["content_sha256"],
                        "index_state": "unchanged",
                    }
                raise ValidationError("event_id already exists with different content or metadata")

            if supersedes:
                target = by_id.get(supersedes)
                if target is None:
                    raise ValidationError("supersedes target does not exist")
                if supersedes not in self._active_event_ids(events):
                    raise ValidationError("supersedes target is not current")
                if target["kind"] != kind or target.get("project_id") != project_id:
                    raise ValidationError("superseding event must preserve kind and project_id")

            event = {
                "schema_version": SCHEMA_VERSION,
                "event_id": event_id,
                **request_identity,
                "created_at": utc_now(),
            }
            encoded = (_canonical_json(event) + "\n").encode("utf-8")
            with self.events_path.open("ab", buffering=0) as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())

            try:
                with closing(self._connect()) as connection:
                    self._create_schema(connection)
                    indexed_count = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                    if indexed_count != len(events):
                        raise sqlite3.IntegrityError("derived index is not current")
                    self._index_event(connection, event, len(events) + 1)
                    self._set_events_metadata(connection, self._events_file_metadata())
                    connection.commit()
            except (OSError, sqlite3.DatabaseError) as exc:
                return {
                    "status": "persisted",
                    "event_id": event_id,
                    "content_sha256": event["content_sha256"],
                    "index_state": "pending_reindex",
                    "warning": f"derived index update failed: {type(exc).__name__}",
                }

        return {
            "status": "created",
            "event_id": event_id,
            "content_sha256": event["content_sha256"],
            "index_state": "current",
        }

    @staticmethod
    def _index_event(connection: sqlite3.Connection, event: dict[str, Any], line_no: int) -> None:
        target = event.get("supersedes")
        if target:
            cursor = connection.execute("UPDATE events SET active=0 WHERE event_id=? AND active=1", (target,))
            if cursor.rowcount != 1:
                raise sqlite3.IntegrityError("supersedes target is absent from or stale in the index")
        active = 0 if event["tombstone"] else 1
        connection.execute(
            """
            INSERT INTO events(
                event_id, line_no, action, kind, project_id, content, created_at,
                source_type, source_ref, supersedes, tombstone, content_sha256, active
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event["event_id"],
                line_no,
                event["action"],
                event["kind"],
                event.get("project_id"),
                event["content"],
                event["created_at"],
                event["source"]["type"],
                event["source"].get("ref"),
                event.get("supersedes"),
                int(event["tombstone"]),
                event["content_sha256"],
                active,
            ),
        )
        if not event["tombstone"] and event["content"]:
            connection.execute(
                "INSERT INTO event_fts(content, event_id) VALUES(?, ?)",
                (event["content"], event["event_id"]),
            )

    def _events_file_metadata(self, *, include_hash: bool = True) -> dict[str, Any]:
        stat = self.events_path.stat()
        metadata: dict[str, Any] = {
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }
        if include_hash:
            metadata["sha256"] = sha256_file(self.events_path)
        return metadata

    @staticmethod
    def _set_events_metadata(connection: sqlite3.Connection, metadata: dict[str, Any]) -> None:
        for key in ("size", "mtime_ns", "sha256"):
            connection.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                (f"events_{key}", str(metadata[key])),
            )

    @staticmethod
    def _normalize_relative_file(relative_path: str) -> str:
        if not isinstance(relative_path, str) or not relative_path.strip():
            raise ValidationError("project file path must be a non-empty string")
        normalized = relative_path.replace("\\", "/")
        pure = PurePosixPath(normalized)
        if pure.is_absolute() or ".." in pure.parts or ":" in normalized or "\0" in normalized:
            raise ValidationError(f"unsafe project file path: {relative_path}")
        canonical = pure.as_posix()
        if canonical in {"", "."}:
            raise ValidationError(f"unsafe project file path: {relative_path}")
        folded_parts = tuple(part.casefold() for part in pure.parts)
        if any(part in EXCLUDED_PATH_PARTS for part in folded_parts):
            raise ValidationError(f"excluded project path: {relative_path}")
        folded_name = pure.name.casefold()
        suffix = PurePosixPath(folded_name).suffix
        if folded_name.startswith(".env"):
            raise ValidationError(f"environment files cannot be registered: {relative_path}")
        if suffix in EXCLUDED_FILE_SUFFIXES or suffix not in ALLOWED_TEXT_SUFFIXES:
            raise ValidationError(f"project source must be an allowed text file: {relative_path}")
        sensitive_name = re.search(
            r"(?:^|[._-])(secret|secrets|credential|credentials|private[_-]?key|token)(?:[._-]|$)",
            folded_name,
        )
        if sensitive_name:
            raise ValidationError(f"sensitive-looking project file cannot be registered: {relative_path}")
        return canonical

    @staticmethod
    def _resolve_project_file(root: Path, relative_path: str, *, require_exists: bool = True) -> Path:
        normalized = ContextHub._normalize_relative_file(relative_path)
        resolved_root = root.resolve(strict=require_exists)
        candidate = (resolved_root / Path(*PurePosixPath(normalized).parts)).resolve(strict=require_exists)
        try:
            common = os.path.commonpath((os.path.normcase(str(resolved_root)), os.path.normcase(str(candidate))))
        except ValueError as exc:
            raise ValidationError("project file resolves to a different volume") from exc
        if common != os.path.normcase(str(resolved_root)):
            raise ValidationError(f"project file escapes the registered root: {relative_path}")
        return candidate

    def register_project(
        self,
        project_id: str,
        root: str | os.PathLike[str],
        files: list[str] | None = None,
    ) -> dict[str, Any]:
        self._require_initialized()
        _validate_project_id(project_id)
        if not isinstance(root, (str, os.PathLike)):
            raise ValidationError("project root must be a path")
        resolved_root = Path(root).expanduser().resolve(strict=True)
        if not resolved_root.is_dir():
            raise ValidationError("project root must be a directory")

        if files is not None:
            if not isinstance(files, list):
                raise ValidationError("files must be a list when supplied")
            requested = list(files)
        else:
            requested = list(DEFAULT_PROJECT_ROOT_FILES)
            context_dir = resolved_root / "context"
            if context_dir.is_dir():
                requested.extend(
                    f"context/{child.name}"
                    for child in sorted(
                        context_dir.iterdir(), key=lambda path: (path.name.casefold(), path.name)
                    )
                    if child.suffix.casefold() == ".md" and child.is_file()
                )
        normalized: list[str] = []
        seen: set[str] = set()
        for item in requested:
            path = self._normalize_relative_file(item)
            key = path.casefold()
            if key in seen:
                continue
            candidate = self._resolve_project_file(resolved_root, path, require_exists=False)
            if candidate.exists():
                if not candidate.is_file():
                    raise ValidationError(f"registered project source is not a file: {path}")
                self._resolve_project_file(resolved_root, path, require_exists=True)
                normalized.append(path)
                seen.add(key)
            elif files is not None:
                raise ValidationError(f"explicit project source does not exist: {path}")

        with ExclusiveFileLock(self.lock_path):
            manifest = self._load_manifest()
            manifest["projects"][project_id] = {
                "root": str(resolved_root),
                "files": normalized,
                "file_metadata": {},
                "registered_at": utc_now(),
            }
            reindex_result = self._reindex_locked(manifest)
        return {
            "ok": True,
            "project_id": project_id,
            "files": normalized,
            "sections": reindex_result["sections"],
        }

    @staticmethod
    def _markdown_sections(content: str) -> list[tuple[str | None, str, int, int, int]]:
        lines = content.splitlines(keepends=True)
        if not lines and content == "":
            return [(None, "", 1, 0, 0)]
        offsets = [0]
        for line in lines:
            offsets.append(offsets[-1] + len(line))
        starts: list[tuple[int, str]] = []
        for index, line in enumerate(lines):
            match = HEADING_RE.match(line)
            if match:
                starts.append((index, match.group(1)))
        if not starts:
            return [(None, content, 1, 0, len(content))]

        sections: list[tuple[str | None, str, int, int, int]] = []
        if starts[0][0] > 0:
            end = starts[0][0]
            sections.append((None, "".join(lines[:end]), 1, 0, offsets[end]))
        for position, (start, heading) in enumerate(starts):
            end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
            sections.append(
                (heading, "".join(lines[start:end]), start + 1, offsets[start], offsets[end])
            )
        return sections

    @staticmethod
    def _metadata_from_bytes(path: Path, raw: bytes, stat: os.stat_result) -> dict[str, Any]:
        return {
            "size": len(raw),
            "mtime_ns": stat.st_mtime_ns,
            "sha256": hashlib.sha256(raw).hexdigest(),
        }

    def _index_project_file(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        root: Path,
        relative_path: str,
    ) -> tuple[int, dict[str, Any]]:
        path = self._resolve_project_file(root, relative_path, require_exists=True)
        stat_before = path.stat()
        raw = path.read_bytes()
        stat_after = path.stat()
        if (stat_before.st_size, stat_before.st_mtime_ns) != (stat_after.st_size, stat_after.st_mtime_ns):
            raise ValidationError(f"registered source changed while indexing: {path}")
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationError(f"registered text source is not UTF-8: {path}") from exc

        metadata = self._metadata_from_bytes(path, raw, stat_after)
        file_hash = metadata["sha256"]
        modified_at = (
            datetime.fromtimestamp(stat_after.st_mtime, timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        count = 0
        for section_index, (heading, section, line_start, char_start, char_end) in enumerate(
            self._markdown_sections(content)
        ):
            section_hash = sha256_text(section)
            ref = f"file:{project_id}:{file_hash[:16]}:{section_index}:{section_hash[:16]}"
            source_ref = f"{project_id}:{relative_path}"
            connection.execute(
                """
                INSERT INTO sections(
                    ref, project_id, relative_path, section_index, heading, content,
                    line_start, char_start, char_end, modified_at, content_sha256,
                    file_sha256, source_ref
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ref,
                    project_id,
                    relative_path,
                    section_index,
                    heading,
                    section,
                    line_start,
                    char_start,
                    char_end,
                    modified_at,
                    section_hash,
                    file_hash,
                    source_ref,
                ),
            )
            if section:
                connection.execute(
                    "INSERT INTO section_fts(content, ref) VALUES(?, ?)",
                    (section, ref),
                )
            count += 1
        return count, metadata

    def _index_manifest_sections(self, connection: sqlite3.Connection, manifest: dict[str, Any]) -> int:
        count = 0
        for project_id in sorted(manifest["projects"]):
            project = manifest["projects"][project_id]
            root = Path(project["root"])
            project["file_metadata"] = {}
            for relative_path in sorted(project.get("files", []), key=str.casefold):
                try:
                    indexed, metadata = self._index_project_file(
                        connection, project_id, root, relative_path
                    )
                except FileNotFoundError:
                    continue
                project["file_metadata"][relative_path] = metadata
                count += indexed
        return count

    @staticmethod
    def _delete_indexed_file(
        connection: sqlite3.Connection, project_id: str, relative_path: str
    ) -> int:
        refs = connection.execute(
            "SELECT ref FROM sections WHERE project_id=? AND relative_path=?",
            (project_id, relative_path),
        ).fetchall()
        for row in refs:
            connection.execute("DELETE FROM section_fts WHERE ref=?", (row["ref"],))
        connection.execute(
            "DELETE FROM sections WHERE project_id=? AND relative_path=?",
            (project_id, relative_path),
        )
        return len(refs)

    def _sync_registered_sources_locked(self, manifest: dict[str, Any]) -> dict[str, Any]:
        """Bring the disposable index in line with authoritative files.

        The caller owns the process-wide lock. Size and mtime are the hot-path
        check; SHA-256 is calculated only when either value changes.
        """

        current_events = self._events_file_metadata(include_hash=False)
        with closing(self._connect()) as connection:
            self._create_schema(connection)
            stored_events = {
                key: connection.execute(
                    "SELECT value FROM meta WHERE key=?", (f"events_{key}",)
                ).fetchone()
                for key in ("size", "mtime_ns", "sha256")
            }
        if any(row is None for row in stored_events.values()):
            return self._reindex_locked(manifest)
        try:
            stored_size = int(stored_events["size"]["value"])
            stored_mtime_ns = int(stored_events["mtime_ns"]["value"])
            stored_sha256 = stored_events["sha256"]["value"]
        except (KeyError, TypeError, ValueError):
            return self._reindex_locked(manifest)
        if (
            stored_size < 0
            or stored_mtime_ns < 0
            or not isinstance(stored_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", stored_sha256) is None
        ):
            return self._reindex_locked(manifest)
        if (
            stored_size != current_events["size"]
            or stored_mtime_ns != current_events["mtime_ns"]
        ):
            return self._reindex_locked(manifest)

        manifest_changed = False
        indexed = 0
        removed = 0
        allowed_sources: set[tuple[str, str]] = set()
        for project_id, project in manifest["projects"].items():
            _validate_project_id(project_id)
            if not isinstance(project, dict) or not isinstance(project.get("root"), str):
                raise ValidationError(f"project {project_id} has an invalid manifest entry")
            if not isinstance(project.get("files"), list):
                raise ValidationError(f"project {project_id} files must be a list")
            for relative_path in project["files"]:
                normalized = self._normalize_relative_file(relative_path)
                if normalized != relative_path:
                    raise ValidationError(f"project {project_id} has a non-canonical file path")
                allowed_sources.add((project_id, relative_path))

        with closing(self._connect()) as connection:
            self._create_schema(connection)
            indexed_sources = {
                (row["project_id"], row["relative_path"])
                for row in connection.execute(
                    "SELECT DISTINCT project_id, relative_path FROM sections"
                )
            }
            for project_id, relative_path in indexed_sources - allowed_sources:
                removed += self._delete_indexed_file(connection, project_id, relative_path)

            for project_id in sorted(manifest["projects"]):
                project = manifest["projects"][project_id]
                root = Path(project["root"])
                metadata_by_path = project.get("file_metadata")
                if not isinstance(metadata_by_path, dict):
                    metadata_by_path = {}
                    project["file_metadata"] = metadata_by_path
                    manifest_changed = True

                for stale_path in set(metadata_by_path) - set(project["files"]):
                    del metadata_by_path[stale_path]
                    manifest_changed = True

                for relative_path in sorted(project["files"], key=str.casefold):
                    stored = metadata_by_path.get(relative_path)
                    try:
                        path = self._resolve_project_file(root, relative_path, require_exists=True)
                    except FileNotFoundError:
                        removed += self._delete_indexed_file(connection, project_id, relative_path)
                        if relative_path in metadata_by_path:
                            del metadata_by_path[relative_path]
                            manifest_changed = True
                        continue
                    if not path.is_file():
                        raise ValidationError(f"registered project source is not a file: {relative_path}")

                    stat = path.stat()
                    if (
                        isinstance(stored, dict)
                        and stored.get("size") == stat.st_size
                        and stored.get("mtime_ns") == stat.st_mtime_ns
                    ):
                        continue

                    raw = path.read_bytes()
                    stat_after = path.stat()
                    if (stat.st_size, stat.st_mtime_ns) != (
                        stat_after.st_size,
                        stat_after.st_mtime_ns,
                    ):
                        raise ValidationError(f"registered source changed while checking: {path}")
                    try:
                        raw.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise ValidationError(f"registered text source is not UTF-8: {path}") from exc
                    current = self._metadata_from_bytes(path, raw, stat_after)

                    if not isinstance(stored, dict) or stored.get("sha256") != current["sha256"]:
                        removed += self._delete_indexed_file(connection, project_id, relative_path)
                        added, current = self._index_project_file(
                            connection, project_id, root, relative_path
                        )
                        indexed += added
                    metadata_by_path[relative_path] = current
                    manifest_changed = True

            connection.commit()

        if manifest_changed:
            manifest["updated_at"] = utc_now()
            self._write_manifest(manifest)
        return {"ok": True, "indexed_sections": indexed, "removed_sections": removed}

    def reindex(self) -> dict[str, Any]:
        self._require_initialized()
        with ExclusiveFileLock(self.lock_path):
            return self._reindex_locked(self._load_manifest())

    def _reindex_locked(self, manifest: dict[str, Any]) -> dict[str, Any]:
        events = self._read_events()
        temporary_path = self.index_path.with_name(f".{self.index_path.name}.{uuid.uuid4().hex}.tmp")
        temporary_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with closing(self._connect(temporary_path, wal=False)) as connection:
                self._create_schema(connection)
                for line_no, event in enumerate(events, start=1):
                    self._index_event(connection, event, line_no)
                sections = self._index_manifest_sections(connection, manifest)
                self._set_events_metadata(connection, self._events_file_metadata())
                connection.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES('rebuilt_at', ?)",
                    (utc_now(),),
                )
                connection.commit()
            for suffix in ("-wal", "-shm"):
                Path(f"{self.index_path}{suffix}").unlink(missing_ok=True)
            os.replace(temporary_path, self.index_path)
            with closing(self._connect()) as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            manifest["updated_at"] = utc_now()
            self._write_manifest(manifest)
            return {"ok": True, "events": len(events), "sections": sections}
        finally:
            temporary_path.unlink(missing_ok=True)
            Path(f"{temporary_path}-wal").unlink(missing_ok=True)
            Path(f"{temporary_path}-shm").unlink(missing_ok=True)

    @staticmethod
    def _escape_like(query: str) -> str:
        return query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    @staticmethod
    def _fts_phrase(query: str) -> str:
        return '"' + query.replace('"', '""') + '"'

    @staticmethod
    def _snippet(content: str, query: str, budget: int) -> str:
        compact = re.sub(r"\s+", " ", content).strip()
        if budget <= 0 or not compact:
            return ""
        if len(compact) <= budget:
            return compact
        if budget == 1:
            return "…"
        index = compact.casefold().find(query.casefold())
        if index < 0:
            index = 0
        inner_budget = max(1, budget - 2)
        start = max(0, index - inner_budget // 3)
        end = min(len(compact), start + inner_budget)
        start = max(0, end - inner_budget)
        prefix = "…" if start else ""
        suffix = "…" if end < len(compact) else ""
        return prefix + compact[start:end] + suffix

    def search(
        self,
        query: str,
        *,
        project_id: str | None = None,
        kinds: list[str] | None = None,
        limit: int = 3,
        max_chars: int = 600,
        include_history: bool = False,
    ) -> dict[str, Any]:
        self._require_initialized()
        if not isinstance(query, str):
            raise ValidationError("search query must be a string")
        query = query.strip()
        if not query:
            raise ValidationError("search query must not be empty")
        if len(query) > 500:
            raise ValidationError("search query exceeds 500 characters")
        project_id = _validate_project_id(project_id)
        kind_values: list[str] | None = None
        if kinds is not None:
            if not isinstance(kinds, list) or not kinds:
                raise ValidationError("kinds must be a non-empty list when supplied")
            if any(not isinstance(kind, str) or kind not in ALLOWED_KINDS for kind in kinds):
                raise ValidationError(f"kinds entries must be among {sorted(ALLOWED_KINDS)}")
            kind_values = list(dict.fromkeys(kinds))
        limit = _bounded_int(limit, name="limit", minimum=1, maximum=5)
        max_chars = _bounded_int(max_chars, name="max_chars", minimum=200, maximum=4000)
        if not isinstance(include_history, bool):
            raise ValidationError("include_history must be a boolean")
        if not self.index_path.exists():
            raise IndexPendingError("derived index is missing; run reindex")

        mode = "bounded-like" if len(query) <= 2 else "fts5-trigram"
        candidates: list[dict[str, Any]] = []
        try:
            with closing(self._connect()) as connection:
                event_filters: list[str] = []
                event_params: list[Any] = []
                if not include_history:
                    event_filters.extend(["e.active=1", "e.tombstone=0"])
                if project_id is not None:
                    event_filters.append("e.project_id=?")
                    event_params.append(project_id)
                if kind_values is not None:
                    placeholders = ",".join("?" for _ in kind_values)
                    event_filters.append(f"e.kind IN ({placeholders})")
                    event_params.extend(kind_values)
                where_events = " AND ".join(event_filters) or "1=1"

                section_filters: list[str] = []
                section_params: list[Any] = []
                if project_id is not None:
                    section_filters.append("s.project_id=?")
                    section_params.append(project_id)
                where_sections = " AND ".join(section_filters) or "1=1"
                fetch_limit = limit * 4

                if mode == "bounded-like":
                    like = f"%{self._escape_like(query)}%"
                    event_rows = connection.execute(
                        f"""
                        SELECT e.*, 0.0 AS rank
                        FROM events e
                        WHERE e.content LIKE ? ESCAPE '\\' AND {where_events}
                        ORDER BY e.line_no DESC LIMIT ?
                        """,
                        [like, *event_params, fetch_limit],
                    ).fetchall()
                    section_rows = (
                        connection.execute(
                            f"""
                            SELECT s.*, 0.25 AS rank
                            FROM sections s
                            WHERE s.content LIKE ? ESCAPE '\\' AND {where_sections}
                            ORDER BY s.project_id, s.relative_path, s.section_index LIMIT ?
                            """,
                            [like, *section_params, fetch_limit],
                        ).fetchall()
                        if kind_values is None
                        else []
                    )
                else:
                    phrase = self._fts_phrase(query)
                    event_rows = connection.execute(
                        f"""
                        SELECT e.*, bm25(event_fts) AS rank
                        FROM event_fts JOIN events e ON e.event_id=event_fts.event_id
                        WHERE event_fts MATCH ? AND {where_events}
                        ORDER BY rank, e.line_no DESC LIMIT ?
                        """,
                        [phrase, *event_params, fetch_limit],
                    ).fetchall()
                    section_rows = (
                        connection.execute(
                            f"""
                            SELECT s.*, bm25(section_fts) AS rank
                            FROM section_fts JOIN sections s ON s.ref=section_fts.ref
                            WHERE section_fts MATCH ? AND {where_sections}
                            ORDER BY rank, s.project_id, s.relative_path, s.section_index LIMIT ?
                            """,
                            [phrase, *section_params, fetch_limit],
                        ).fetchall()
                        if kind_values is None
                        else []
                    )

                for row in event_rows:
                    candidates.append(
                        {
                            "rank": float(row["rank"]),
                            "order": -int(row["line_no"]),
                            "ref": f"event:{row['event_id']}",
                            "kind": row["kind"],
                            "project_id": row["project_id"],
                            "time": row["created_at"],
                            "superseded": not bool(row["active"]),
                            "source": {"type": row["source_type"], "ref": row["source_ref"]},
                            "content": row["content"],
                            "score": 1.0 if mode == "bounded-like" else round(abs(float(row["rank"])), 9),
                        }
                    )
                for row in section_rows:
                    candidates.append(
                        {
                            "rank": float(row["rank"]),
                            "order": int(row["section_index"]),
                            "ref": row["ref"],
                            "kind": "markdown",
                            "project_id": row["project_id"],
                            "time": row["modified_at"],
                            "superseded": False,
                            "source": {"type": "project_file", "ref": row["source_ref"]},
                            "content": row["content"],
                            "score": 0.9 if mode == "bounded-like" else round(abs(float(row["rank"])), 9),
                        }
                    )
        except sqlite3.DatabaseError as exc:
            raise IndexPendingError("derived index query failed; run reindex") from exc

        candidates.sort(key=lambda item: (item["rank"], item["order"], item["ref"]))
        selected = candidates[:limit]
        remaining_chars = max_chars
        items = []
        for index, candidate in enumerate(selected):
            remaining_items = len(selected) - index
            snippet_budget = remaining_chars // remaining_items
            snippet = self._snippet(candidate["content"], query, snippet_budget)
            remaining_chars -= len(snippet)
            items.append(
                {
                    "ref": candidate["ref"],
                    "kind": candidate["kind"],
                    "project_id": candidate["project_id"],
                    "time": candidate["time"],
                    "superseded": candidate["superseded"],
                    "score": candidate["score"],
                    "source": candidate["source"],
                    "snippet": snippet,
                }
            )
        return {"op": "search", "query": query, "mode": mode, "items": items}

    def read(self, ref: str | None = None, *, cursor: str | None = None, max_chars: int = 600) -> dict[str, Any]:
        self._require_initialized()
        max_chars = _bounded_int(max_chars, name="max_chars", minimum=200, maximum=4000)
        offset = 0
        cursor_hash: str | None = None
        if cursor is not None:
            payload = _decode_cursor(cursor)
            cursor_ref = payload.get("ref")
            if ref is not None and ref != cursor_ref:
                raise ValidationError("cursor does not belong to the requested ref")
            ref = cursor_ref
            offset = payload.get("offset")
            cursor_hash = payload.get("sha256")
            if not isinstance(ref, str) or not isinstance(offset, int) or offset < 0 or not isinstance(cursor_hash, str):
                raise ValidationError("malformed read cursor")
        if not isinstance(ref, str) or not ref:
            raise ValidationError("read requires ref or cursor")

        try:
            with closing(self._connect()) as connection:
                if ref.startswith("event:"):
                    event_id = ref.removeprefix("event:")
                    row = connection.execute(
                        """SELECT content, content_sha256, kind, project_id, created_at,
                                  source_type, source_ref, active
                           FROM events WHERE event_id=?""",
                        (event_id,),
                    ).fetchone()
                    if row is None:
                        raise NotFoundError(f"unknown ref: {ref}")
                    content = row["content"]
                    content_hash = row["content_sha256"]
                    metadata = {
                        "kind": row["kind"],
                        "project_id": row["project_id"],
                        "time": row["created_at"],
                        "superseded": not bool(row["active"]),
                        "source": {"type": row["source_type"], "ref": row["source_ref"]},
                    }
                elif ref.startswith("file:"):
                    row = connection.execute(
                        """SELECT content, content_sha256, project_id, source_ref,
                                  relative_path, heading, line_start, char_start,
                                  char_end, modified_at
                           FROM sections WHERE ref=?""",
                        (ref,),
                    ).fetchone()
                    if row is None:
                        raise NotFoundError(f"unknown ref: {ref}")
                    content = row["content"]
                    content_hash = row["content_sha256"]
                    metadata = {
                        "kind": "markdown",
                        "project_id": row["project_id"],
                        "time": row["modified_at"],
                        "superseded": False,
                        "source": {"type": "project_file", "ref": row["source_ref"]},
                        "location": {
                            "path": row["relative_path"],
                            "heading": row["heading"],
                            "line_start": row["line_start"],
                            "char_start": row["char_start"],
                            "char_end": row["char_end"],
                        },
                    }
                else:
                    raise ValidationError("ref must start with event: or file:")
        except sqlite3.DatabaseError as exc:
            raise IndexPendingError("derived index read failed; run reindex") from exc

        if cursor_hash is not None and cursor_hash != content_hash:
            raise ValidationError("referenced content changed after the cursor was issued")
        if offset > len(content):
            raise ValidationError("cursor offset is beyond the content length")
        end = min(len(content), offset + max_chars)
        truncated = end < len(content)
        next_cursor = (
            _encode_cursor({"v": 1, "ref": ref, "offset": end, "sha256": content_hash}) if truncated else None
        )
        return {
            "op": "read",
            "ref": ref,
            **metadata,
            "content": content[offset:end],
            "char_offset": offset,
            "total_chars": len(content),
            "sha256": content_hash,
            "next_cursor": next_cursor,
            "truncated": truncated,
        }

    def manifest(self) -> dict[str, Any]:
        manifest = self._load_manifest()
        projects = [
            {"project_id": project_id, "files": list(project.get("files", []))}
            for project_id, project in sorted(manifest["projects"].items())
        ]
        return {
            "op": "manifest",
            "schema_version": manifest["schema_version"],
            "updated_at": manifest["updated_at"],
            "projects": projects,
        }

    def get(self, op: str, **kwargs: Any) -> dict[str, Any]:
        if op == "search":
            return self.search(**kwargs)
        if op == "read":
            return self.read(**kwargs)
        if op == "manifest":
            unexpected = {key for key, value in kwargs.items() if value is not None}
            if unexpected:
                raise ValidationError(f"manifest does not accept {sorted(unexpected)}")
            return self.manifest()
        raise ValidationError("op must be search, read, or manifest")

    def doctor(self, *, reindex: bool = False) -> dict[str, Any]:
        self._require_initialized()
        repair: dict[str, Any] | None = self.reindex() if reindex else None
        errors: list[str] = []
        warnings: list[str] = []
        counts = {
            "events": 0,
            "indexed_events": 0,
            "event_fts": 0,
            "sections": 0,
            "section_fts": 0,
            "projects": 0,
        }
        manifest_valid = False
        try:
            manifest = self._load_manifest()
            if not isinstance(manifest.get("updated_at"), str):
                raise ValidationError("manifest.json is missing updated_at")
            counts["projects"] = len(manifest["projects"])
            manifest_valid = True
        except (ValidationError, OSError) as exc:
            manifest = None
            errors.append(str(exc))

        events_valid = False
        try:
            events = self._read_events()
            counts["events"] = len(events)
            known: set[str] = set()
            active_so_far: set[str] = set()
            for event in events:
                target = event.get("supersedes")
                if target and target not in known:
                    errors.append(f"event {event['event_id']} supersedes an unknown or later event")
                elif target and target not in active_so_far:
                    errors.append(f"event {event['event_id']} supersedes a non-current event")
                if target:
                    active_so_far.discard(target)
                if not event["tombstone"]:
                    active_so_far.add(event["event_id"])
                known.add(event["event_id"])
            events_valid = True
        except (ValidationError, OSError) as exc:
            events = []
            errors.append(str(exc))

        if not self.index_path.exists():
            errors.append("derived index is missing")
        else:
            try:
                with closing(self._connect()) as connection:
                    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
                    if integrity != "ok":
                        errors.append(f"SQLite integrity_check: {integrity}")
                    schema_row = connection.execute(
                        "SELECT value FROM meta WHERE key='schema_version'"
                    ).fetchone()
                    if schema_row is None or schema_row["value"] != str(SCHEMA_VERSION):
                        errors.append("derived index schema_version mismatch")

                    actual_event_rows = connection.execute(
                        """SELECT event_id, line_no, action, kind, project_id, content,
                                  created_at, source_type, source_ref, supersedes,
                                  tombstone, content_sha256, active
                           FROM events ORDER BY line_no"""
                    ).fetchall()
                    actual_event_fts_rows = connection.execute(
                        "SELECT event_id, content FROM event_fts"
                    ).fetchall()
                    actual_section_rows = connection.execute(
                        """SELECT ref, project_id, relative_path, section_index, heading,
                                  content, line_start, char_start, char_end, modified_at,
                                  content_sha256, file_sha256, source_ref
                           FROM sections"""
                    ).fetchall()
                    actual_section_fts_rows = connection.execute(
                        "SELECT ref, content FROM section_fts"
                    ).fetchall()
                    counts["indexed_events"] = len(actual_event_rows)
                    counts["event_fts"] = len(actual_event_fts_rows)
                    counts["sections"] = len(actual_section_rows)
                    counts["section_fts"] = len(actual_section_fts_rows)

                    if events_valid:
                        active_ids = self._active_event_ids(events)
                        expected_events = {
                            event["event_id"]: {
                                "event_id": event["event_id"],
                                "line_no": line_no,
                                "action": event["action"],
                                "kind": event["kind"],
                                "project_id": event["project_id"],
                                "content": event["content"],
                                "created_at": event["created_at"],
                                "source_type": event["source"]["type"],
                                "source_ref": event["source"]["ref"],
                                "supersedes": event["supersedes"],
                                "tombstone": int(event["tombstone"]),
                                "content_sha256": event["content_sha256"],
                                "active": int(event["event_id"] in active_ids),
                            }
                            for line_no, event in enumerate(events, start=1)
                        }
                        actual_events = {
                            row["event_id"]: dict(row) for row in actual_event_rows
                        }
                        if set(actual_events) != set(expected_events):
                            errors.append("derived event index IDs do not match events.jsonl")
                        for event_id in sorted(set(actual_events) & set(expected_events)):
                            if actual_events[event_id] != expected_events[event_id]:
                                errors.append(f"derived event row mismatch: {event_id}")

                        expected_event_fts = sorted(
                            (event["event_id"], event["content"])
                            for event in events
                            if not event["tombstone"] and event["content"]
                        )
                        actual_event_fts = sorted(
                            (row["event_id"], row["content"])
                            for row in actual_event_fts_rows
                        )
                        if actual_event_fts != expected_event_fts:
                            errors.append("event FTS index does not match events.jsonl")

                        expected_event_metadata = self._events_file_metadata()
                        for key, value in expected_event_metadata.items():
                            row = connection.execute(
                                "SELECT value FROM meta WHERE key=?", (f"events_{key}",)
                            ).fetchone()
                            if row is None or row["value"] != str(value):
                                errors.append(f"events.jsonl {key} metadata mismatch")

                    if manifest_valid and manifest is not None:
                        expected_sections: dict[str, dict[str, Any]] = {}
                        expected_section_fts: list[tuple[str, str]] = []
                        allowed_sources: set[tuple[str, str]] = set()
                        for project_id, project in sorted(manifest["projects"].items()):
                            try:
                                _validate_project_id(project_id)
                                if not isinstance(project, dict):
                                    raise ValidationError("project entry must be an object")
                                root_value = project.get("root")
                                files = project.get("files")
                                metadata_by_path = project.get("file_metadata")
                                if not isinstance(root_value, str) or not Path(root_value).is_absolute():
                                    raise ValidationError("project root must be absolute")
                                if not isinstance(files, list):
                                    raise ValidationError("project files must be a list")
                                if not isinstance(metadata_by_path, dict):
                                    raise ValidationError("project file_metadata must be an object")
                                root = Path(root_value).resolve(strict=True)
                                if not root.is_dir():
                                    raise ValidationError("project root is not a directory")
                            except (FileNotFoundError, OSError, ValidationError) as exc:
                                errors.append(f"project {project_id}: {exc}")
                                continue

                            normalized_files: list[str] = []
                            seen_paths: set[str] = set()
                            for raw_path in files:
                                try:
                                    relative_path = self._normalize_relative_file(raw_path)
                                    if relative_path != raw_path:
                                        raise ValidationError("file path is not canonical")
                                    folded = relative_path.casefold()
                                    if folded in seen_paths:
                                        raise ValidationError("duplicate file path ignoring case")
                                    seen_paths.add(folded)
                                    normalized_files.append(relative_path)
                                    allowed_sources.add((project_id, relative_path))
                                except ValidationError as exc:
                                    errors.append(f"{project_id}:{raw_path}: {exc}")

                            if set(metadata_by_path) - set(normalized_files):
                                errors.append(f"project {project_id} has stale file metadata")

                            for relative_path in normalized_files:
                                try:
                                    path = self._resolve_project_file(
                                        root, relative_path, require_exists=True
                                    )
                                    if not path.is_file():
                                        raise ValidationError("registered source is not a file")
                                    stat_before = path.stat()
                                    raw = path.read_bytes()
                                    stat_after = path.stat()
                                    if (stat_before.st_size, stat_before.st_mtime_ns) != (
                                        stat_after.st_size,
                                        stat_after.st_mtime_ns,
                                    ):
                                        raise ValidationError("source changed during doctor")
                                    content = raw.decode("utf-8")
                                except FileNotFoundError as exc:
                                    warnings.append(f"{project_id}:{relative_path}: {exc}")
                                    continue
                                except UnicodeDecodeError:
                                    errors.append(
                                        f"{project_id}:{relative_path}: registered source is not UTF-8"
                                    )
                                    continue
                                except (OSError, ValidationError) as exc:
                                    errors.append(f"{project_id}:{relative_path}: {exc}")
                                    continue

                                current_metadata = self._metadata_from_bytes(
                                    path, raw, stat_after
                                )
                                if metadata_by_path.get(relative_path) != current_metadata:
                                    errors.append(
                                        f"{project_id}:{relative_path}: manifest metadata mismatch"
                                    )
                                file_hash = current_metadata["sha256"]
                                modified_at = (
                                    datetime.fromtimestamp(stat_after.st_mtime, timezone.utc)
                                    .isoformat(timespec="milliseconds")
                                    .replace("+00:00", "Z")
                                )
                                for section_index, (
                                    heading,
                                    section,
                                    line_start,
                                    char_start,
                                    char_end,
                                ) in enumerate(self._markdown_sections(content)):
                                    section_hash = sha256_text(section)
                                    ref = (
                                        f"file:{project_id}:{file_hash[:16]}:"
                                        f"{section_index}:{section_hash[:16]}"
                                    )
                                    expected_sections[ref] = {
                                        "ref": ref,
                                        "project_id": project_id,
                                        "relative_path": relative_path,
                                        "section_index": section_index,
                                        "heading": heading,
                                        "content": section,
                                        "line_start": line_start,
                                        "char_start": char_start,
                                        "char_end": char_end,
                                        "modified_at": modified_at,
                                        "content_sha256": section_hash,
                                        "file_sha256": file_hash,
                                        "source_ref": f"{project_id}:{relative_path}",
                                    }
                                    if section:
                                        expected_section_fts.append((ref, section))

                        actual_sources = {
                            (row["project_id"], row["relative_path"])
                            for row in actual_section_rows
                        }
                        if not actual_sources.issubset(allowed_sources):
                            errors.append("derived index contains a non-allow-listed source")
                        actual_sections = {
                            row["ref"]: dict(row) for row in actual_section_rows
                        }
                        if set(actual_sections) != set(expected_sections):
                            errors.append("derived Markdown refs do not match registered sources")
                        for ref in sorted(set(actual_sections) & set(expected_sections)):
                            if actual_sections[ref] != expected_sections[ref]:
                                errors.append(f"derived Markdown row mismatch: {ref}")
                        actual_section_fts = sorted(
                            (row["ref"], row["content"])
                            for row in actual_section_fts_rows
                        )
                        if actual_section_fts != sorted(expected_section_fts):
                            errors.append("Markdown FTS index does not match registered sources")
            except (OSError, sqlite3.DatabaseError) as exc:
                errors.append(f"SQLite error: {exc}")
        result: dict[str, Any] = {
            "ok": not errors,
            "errors": errors,
            "warnings": warnings,
            "counts": counts,
        }
        if repair is not None:
            result["reindex"] = repair
        return result

    def backup(self, destination: str | os.PathLike[str] | None = None) -> dict[str, Any]:
        self._require_initialized()
        destination_dir = Path(destination).expanduser().resolve() if destination else self.backups_dir
        destination_dir.mkdir(parents=True, exist_ok=True)
        backup_path = destination_dir / f"context-hub-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.zip"
        sources = [self.config_path, self.manifest_path, self.events_path]
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{backup_path.name}.", suffix=".tmp", dir=destination_dir
        )
        os.close(file_descriptor)
        temporary_path = Path(temporary_name)
        with ExclusiveFileLock(self.lock_path):
            try:
                entries = []
                for source in sources:
                    relative = source.relative_to(self.root).as_posix()
                    entries.append(
                        {
                            "path": relative,
                            "sha256": sha256_file(source),
                            "bytes": source.stat().st_size,
                        }
                    )
                backup_manifest = {
                    "schema_version": SCHEMA_VERSION,
                    "created_at": utc_now(),
                    "entries": entries,
                }
                with zipfile.ZipFile(
                    temporary_path, "w", compression=zipfile.ZIP_DEFLATED
                ) as archive:
                    for source in sources:
                        archive.write(source, source.relative_to(self.root).as_posix())
                    archive.writestr(
                        "backup-manifest.json",
                        json.dumps(backup_manifest, ensure_ascii=False, indent=2) + "\n",
                    )
                with temporary_path.open("r+b") as handle:
                    os.fsync(handle.fileno())
                os.replace(temporary_path, backup_path)
            finally:
                temporary_path.unlink(missing_ok=True)
        return {
            "ok": True,
            "backup": str(backup_path),
            "sha256": sha256_file(backup_path),
            "entries": entries,
        }

    def remove_index_for_test(self) -> None:
        """Test-only helper; callers still have to invoke reindex explicitly."""
        for suffix in ("", "-wal", "-shm"):
            Path(f"{self.index_path}{suffix}").unlink(missing_ok=True)

"""Event log, manifest and SQLite connection handling. Part of ContextHub; code moved verbatim from hub.py."""

from __future__ import annotations

from ._common import (  # noqa: F401  (moved verbatim from hub.py)
    base64,
    hashlib,
    json,
    os,
    re,
    shutil,
    sqlite3,
    stat,
    tempfile,
    tomllib,
    uuid,
    zipfile,
    closing,
    datetime,
    timezone,
    Path,
    PurePosixPath,
    Any,
    Iterable,
    IndexPendingError,
    NotFoundError,
    ValidationError,
    ExclusiveFileLock,
    SCHEMA_VERSION,
    BACKUP_SOURCE_PATHS,
    BACKUP_MANIFEST_PATH,
    MAX_BACKUP_ENTRY_BYTES,
    MAX_BACKUP_TOTAL_BYTES,
    MAX_BACKUP_COMPRESSION_RATIO,
    ALLOWED_ACTIONS,
    ALLOWED_KINDS,
    DEFAULT_PROJECT_ROOT_FILES,
    ALLOWED_TEXT_SUFFIXES,
    EXCLUDED_PATH_PARTS,
    EXCLUDED_FILE_SUFFIXES,
    PROJECT_ID_RE,
    EVENT_ID_RE,
    HEADING_RE,
    default_data_dir,
    sha256_text,
    sha256_file,
    utc_now,
    _canonical_json,
    _atomic_write_text,
    _bounded_int,
    _validate_project_id,
    _normalize_event_id,
    _encode_cursor,
    _decode_cursor,
)


class StoreMixin:
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

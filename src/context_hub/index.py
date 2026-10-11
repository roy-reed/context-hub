"""FTS5 index schema, source indexing and rebuild. Part of ContextHub; code moved verbatim from hub.py."""

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


class IndexMixin:
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

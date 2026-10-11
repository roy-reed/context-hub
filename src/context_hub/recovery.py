"""Health checks, backup and restore. Part of ContextHub; code moved verbatim from hub.py."""

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


class RecoveryMixin:
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
        sources = [self.root / relative for relative in BACKUP_SOURCE_PATHS]
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
                    "scope": "authoritative-sources-only",
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

    @classmethod
    def restore(
        cls,
        archive_path: str | os.PathLike[str],
        destination: str | os.PathLike[str],
    ) -> dict[str, Any]:
        """Verify an authoritative backup, restore it atomically, and rebuild the index."""
        backup = Path(archive_path).expanduser().resolve()
        target = Path(destination).expanduser().resolve()
        if not backup.is_file():
            raise NotFoundError(f"backup archive not found: {backup}")
        if target.exists():
            raise ValidationError("restore destination must not already exist")
        target.parent.mkdir(parents=True, exist_ok=True)

        expected_members = {*BACKUP_SOURCE_PATHS, BACKUP_MANIFEST_PATH}
        staged_path = Path(
            tempfile.mkdtemp(prefix=f".{target.name}.restore-", dir=target.parent)
        ).resolve()
        moved = False
        verified_entries: list[dict[str, Any]] = []
        try:
            try:
                archive = zipfile.ZipFile(backup)
            except (OSError, zipfile.BadZipFile) as exc:
                raise ValidationError("backup is not a readable ZIP archive") from exc

            with archive:
                infos = archive.infolist()
                names = [info.filename for info in infos]
                if len(names) != len(set(names)):
                    raise ValidationError("backup contains duplicate member names")
                if set(names) != expected_members or len(names) != len(expected_members):
                    raise ValidationError("backup contains missing or unexpected members")
                for info in infos:
                    member = PurePosixPath(info.filename)
                    if member.is_absolute() or ".." in member.parts or "\\" in info.filename:
                        raise ValidationError("backup contains an unsafe member path")
                    if info.flag_bits & 0x1:
                        raise ValidationError("backup members must not be encrypted")
                    mode = info.external_attr >> 16
                    file_type = stat.S_IFMT(mode)
                    if info.is_dir() or file_type not in (0, stat.S_IFREG):
                        raise ValidationError("backup members must be regular files")
                    if info.file_size > MAX_BACKUP_ENTRY_BYTES:
                        raise ValidationError("backup member exceeds the restore size limit")
                    if info.file_size and info.compress_size == 0:
                        raise ValidationError("backup member has an invalid compression size")
                    if (
                        info.compress_size
                        and info.file_size / info.compress_size > MAX_BACKUP_COMPRESSION_RATIO
                    ):
                        raise ValidationError("backup member exceeds the compression-ratio limit")
                if sum(info.file_size for info in infos) > MAX_BACKUP_TOTAL_BYTES:
                    raise ValidationError("backup exceeds the total restore size limit")

                try:
                    backup_manifest = json.loads(archive.read(BACKUP_MANIFEST_PATH))
                except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValidationError("backup manifest is missing or malformed") from exc
                if not isinstance(backup_manifest, dict):
                    raise ValidationError("backup manifest must be an object")
                if backup_manifest.get("schema_version") != SCHEMA_VERSION:
                    raise ValidationError("unsupported backup schema version")
                if backup_manifest.get("scope") != "authoritative-sources-only":
                    raise ValidationError("unsupported backup scope")
                entries = backup_manifest.get("entries")
                if not isinstance(entries, list) or len(entries) != len(BACKUP_SOURCE_PATHS):
                    raise ValidationError("backup manifest entries are incomplete")

                manifest_entries: dict[str, dict[str, Any]] = {}
                for entry in entries:
                    if not isinstance(entry, dict):
                        raise ValidationError("backup manifest entry must be an object")
                    path = entry.get("path")
                    digest = entry.get("sha256")
                    byte_count = entry.get("bytes")
                    if path in manifest_entries:
                        raise ValidationError("backup manifest contains duplicate paths")
                    if path not in BACKUP_SOURCE_PATHS:
                        raise ValidationError("backup manifest contains an unexpected path")
                    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                        raise ValidationError("backup manifest contains an invalid SHA-256")
                    if (
                        isinstance(byte_count, bool)
                        or not isinstance(byte_count, int)
                        or not 0 <= byte_count <= MAX_BACKUP_ENTRY_BYTES
                    ):
                        raise ValidationError("backup manifest contains an invalid byte count")
                    manifest_entries[path] = entry
                if set(manifest_entries) != set(BACKUP_SOURCE_PATHS):
                    raise ValidationError("backup manifest does not cover authoritative sources exactly")

                for relative in BACKUP_SOURCE_PATHS:
                    entry = manifest_entries[relative]
                    restored_file = staged_path.joinpath(*PurePosixPath(relative).parts)
                    restored_file.parent.mkdir(parents=True, exist_ok=True)
                    digest = hashlib.sha256()
                    byte_count = 0
                    with archive.open(relative) as source, restored_file.open("xb") as output:
                        while chunk := source.read(1024 * 1024):
                            byte_count += len(chunk)
                            if byte_count > MAX_BACKUP_ENTRY_BYTES:
                                raise ValidationError(
                                    f"backup member exceeds the restore size limit: {relative}"
                                )
                            digest.update(chunk)
                            output.write(chunk)
                    if byte_count != entry["bytes"]:
                        raise ValidationError(f"backup byte count mismatch: {relative}")
                    if digest.hexdigest() != entry["sha256"]:
                        raise ValidationError(f"backup SHA-256 mismatch: {relative}")
                    verified_entries.append(
                        {"path": relative, "sha256": entry["sha256"], "bytes": entry["bytes"]}
                    )

            staged_hub = cls(staged_path)
            staged_hub.initialize()
            diagnostic = staged_hub.doctor(reindex=True)
            if not diagnostic["ok"]:
                raise ValidationError(
                    "restored authoritative sources failed validation: "
                    + "; ".join(diagnostic["errors"])
                )
            os.replace(staged_path, target)
            moved = True
            final_diagnostic = cls(target).doctor()
            if not final_diagnostic["ok"]:
                raise ValidationError("restored index failed post-move validation")
            return {
                "ok": True,
                "backup": str(backup),
                "destination": str(target),
                "backup_sha256": sha256_file(backup),
                "entries": verified_entries,
                "reindexed": True,
                "doctor": final_diagnostic,
            }
        finally:
            if not moved and staged_path.exists() and staged_path.parent == target.parent:
                shutil.rmtree(staged_path)

    def remove_index_for_test(self) -> None:
        """Test-only helper; callers still have to invoke reindex explicitly."""
        for suffix in ("", "-wal", "-shm"):
            Path(f"{self.index_path}{suffix}").unlink(missing_ok=True)

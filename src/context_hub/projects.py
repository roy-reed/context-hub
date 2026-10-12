"""Project registration and project-relative path validation. Part of ContextHub; code moved verbatim from hub.py."""

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


class ProjectsMixin:
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
        normalized = ProjectsMixin._normalize_relative_file(relative_path)
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

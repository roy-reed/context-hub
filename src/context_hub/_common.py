"""Shared constants and helpers for :mod:`context_hub.hub` (moved verbatim from hub.py)."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
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
BACKUP_SOURCE_PATHS = ("config.toml", "manifest.json", "memory/events.jsonl")
BACKUP_MANIFEST_PATH = "backup-manifest.json"
MAX_BACKUP_ENTRY_BYTES = 512 * 1024 * 1024
MAX_BACKUP_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_BACKUP_COMPRESSION_RATIO = 1000
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

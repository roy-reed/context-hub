"""Authoritative JSONL/Markdown store and rebuildable SQLite index.

ContextHub is composed from mixins in store/index/projects/query/recovery; module-level
constants and helpers live in _common and are re-exported here for backward compatibility.
"""

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
from .index import IndexMixin
from .projects import ProjectsMixin
from .query import QueryMixin
from .recovery import RecoveryMixin
from .store import StoreMixin


class ContextHub(StoreMixin, IndexMixin, ProjectsMixin, QueryMixin, RecoveryMixin):
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
                config = (
                    f"schema_version = {SCHEMA_VERSION}\n"
                    f"write_enabled = {str(write_enabled).lower()}\n"
                    "\n"
                    "[loop]\n"
                    'classification = "synthetic"\n'
                    "min_sync_interval_seconds = 300\n"
                    "max_stale_seconds = 900\n"
                    "max_backup_age_seconds = 3600\n"
                    "\n"
                    "[telemetry]\n"
                    "enabled = false\n"
                    "max_bytes = 1048576\n"
                )
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

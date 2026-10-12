"""Search, read and manifest queries. Part of ContextHub; code moved verbatim from hub.py."""

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


class QueryMixin:
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

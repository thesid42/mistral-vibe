from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
import re
import sqlite3
import threading

from vibe.core.vibevm.models import ContextPage, PageState, VMStats

_SCHEMA_PAGES = """
CREATE TABLE IF NOT EXISTS pages (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, tool_call_id TEXT UNIQUE NOT NULL,
  page_type TEXT, content TEXT, summary TEXT, source_path TEXT, source_hash TEXT,
  token_count INTEGER, importance REAL, access_count INTEGER DEFAULT 0,
  created_seq INTEGER, created_at TEXT, last_accessed TEXT, state TEXT)
"""
_SCHEMA_STATS = """
CREATE TABLE IF NOT EXISTS stats (key TEXT PRIMARY KEY, value INTEGER DEFAULT 0)
"""
_SCHEMA_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS page_fts USING fts5(page_id UNINDEXED, summary, content)
"""

_FTS_RESERVED = frozenset({"AND", "OR", "NOT", "NEAR"})
_TERM_PATTERN = re.compile(r"\w+")
_MIN_FILENAME_TERM_LEN = 3


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def page_from_row(row: sqlite3.Row) -> ContextPage:
    return ContextPage(
        id=row["id"],
        session_id=row["session_id"],
        tool_call_id=row["tool_call_id"],
        page_type=row["page_type"],
        content=row["content"],
        summary=row["summary"],
        source_path=row["source_path"],
        source_hash=row["source_hash"],
        token_count=row["token_count"],
        importance=row["importance"],
        access_count=row["access_count"],
        created_seq=row["created_seq"],
        created_at=row["created_at"],
        last_accessed=row["last_accessed"],
        state=PageState(row["state"]),
    )


def stats_from_rows(rows: Iterable[sqlite3.Row]) -> VMStats:
    known = VMStats.model_fields
    values = {row["key"]: row["value"] for row in rows if row["key"] in known}
    return VMStats(**values)


def _extract_terms(query: str) -> list[str]:
    return [t for t in _TERM_PATTERN.findall(query) if t.upper() not in _FTS_RESERVED]


def _sanitize_fts_query(query: str) -> str:
    return " OR ".join(_extract_terms(query))


def filename_match_terms(query: str) -> list[str]:
    """Query terms usable for filename matching: lowercased, length >= 3.

    Short terms like "py" from "auth.py" are excluded so they don't boost
    every Python file in the store.
    """
    lowered = (t.lower() for t in _extract_terms(query))
    return [t for t in lowered if len(t) >= _MIN_FILENAME_TERM_LEN]


def page_matches_filename(page: ContextPage, terms: list[str]) -> bool:
    """True if a filename term is a substring of page.source_path's basename."""
    if not page.source_path:
        return False
    basename = Path(page.source_path).name.lower()
    return any(term in basename for term in terms)


def _rank_by_filename(pages: list[ContextPage], terms: list[str]) -> list[ContextPage]:
    """Stable-partition pages so filename matches come first, order preserved."""
    if not terms:
        return pages
    matched = [p for p in pages if page_matches_filename(p, terms)]
    unmatched = [p for p in pages if not page_matches_filename(p, terms)]
    return matched + unmatched


class PageStore:
    """SQLite-backed store for one session's context pages.

    One connection per store, WAL mode, ``check_same_thread=False`` so the
    agent loop and the ``recall_context`` tool (same process) can share it;
    writes are serialized through ``_lock`` to keep transactions short and
    non-overlapping.
    """

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self.fts_enabled = True
        with self._conn:
            self._conn.execute(_SCHEMA_PAGES)
            self._conn.execute(_SCHEMA_STATS)
            try:
                self._conn.execute(_SCHEMA_FTS)
            except sqlite3.OperationalError:
                self.fts_enabled = False

    def upsert_page(self, page: ContextPage) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO pages (
                    id, session_id, tool_call_id, page_type, content, summary,
                    source_path, source_hash, token_count, importance,
                    access_count, created_seq, created_at, last_accessed, state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tool_call_id) DO UPDATE SET
                    page_type = excluded.page_type,
                    content = excluded.content,
                    summary = excluded.summary,
                    source_path = excluded.source_path,
                    source_hash = excluded.source_hash,
                    token_count = excluded.token_count,
                    importance = excluded.importance,
                    access_count = excluded.access_count,
                    created_seq = excluded.created_seq,
                    created_at = excluded.created_at,
                    last_accessed = excluded.last_accessed,
                    state = excluded.state
                """,
                (
                    page.id,
                    page.session_id,
                    page.tool_call_id,
                    page.page_type,
                    page.content,
                    page.summary,
                    page.source_path,
                    page.source_hash,
                    page.token_count,
                    page.importance,
                    page.access_count,
                    page.created_seq,
                    page.created_at,
                    page.last_accessed,
                    page.state.value,
                ),
            )
            if self.fts_enabled:
                self._conn.execute("DELETE FROM page_fts WHERE page_id = ?", (page.id,))
                self._conn.execute(
                    "INSERT INTO page_fts (page_id, summary, content) VALUES (?, ?, ?)",
                    (page.id, page.summary, page.content),
                )

    def get_by_tool_call_id(self, tcid: str) -> ContextPage | None:
        row = self._conn.execute(
            "SELECT * FROM pages WHERE tool_call_id = ?", (tcid,)
        ).fetchone()
        return page_from_row(row) if row is not None else None

    def get_by_page_id(self, pid: str) -> ContextPage | None:
        row = self._conn.execute("SELECT * FROM pages WHERE id = ?", (pid,)).fetchone()
        return page_from_row(row) if row is not None else None

    def all_pages(self) -> list[ContextPage]:
        rows = self._conn.execute("SELECT * FROM pages ORDER BY created_seq").fetchall()
        return [page_from_row(row) for row in rows]

    def set_state(self, pid: str, state: PageState) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE pages SET state = ? WHERE id = ?", (state.value, pid)
            )

    def touch(self, pid: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE pages SET access_count = access_count + 1, "
                "last_accessed = ? WHERE id = ?",
                (utc_now_iso(), pid),
            )

    def search(self, query: str, limit: int = 5) -> list[ContextPage]:
        pool_size = max(limit * 3, 10)
        if self.fts_enabled:
            sanitized = _sanitize_fts_query(query)
            if sanitized:
                try:
                    rows = self._conn.execute(
                        "SELECT p.* FROM page_fts f JOIN pages p ON p.id = f.page_id "
                        "WHERE f MATCH ? ORDER BY f.rank LIMIT ?",
                        (sanitized, pool_size),
                    ).fetchall()
                    pages = [page_from_row(row) for row in rows]
                    return _rank_by_filename(pages, filename_match_terms(query))[:limit]
                except sqlite3.OperationalError:
                    pass
        return self._search_like(query, limit)

    def _search_like(self, query: str, limit: int) -> list[ContextPage]:
        terms = [t.lower() for t in _extract_terms(query)]
        if not terms:
            return []
        rows = self._conn.execute("SELECT * FROM pages").fetchall()
        scored: list[tuple[int, ContextPage]] = []
        for row in rows:
            page = page_from_row(row)
            haystack = f"{page.summary}\n{page.content}".lower()
            hits = sum(haystack.count(term) for term in terms)
            if hits:
                scored.append((hits, page))
        scored.sort(key=lambda item: item[0], reverse=True)
        pool_size = max(limit * 3, 10)
        pool = [page for _, page in scored[:pool_size]]
        return _rank_by_filename(pool, filename_match_terms(query))[:limit]

    def next_page_id(self) -> str:
        row = self._conn.execute(
            "SELECT MAX(CAST(SUBSTR(id, 2) AS INTEGER)) FROM pages"
        ).fetchone()
        current = row[0] if row is not None and row[0] is not None else 0
        return f"P{current + 1:03d}"

    def bump_stat(self, key: str, by: int = 1) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO stats (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = value + excluded.value",
                (key, by),
            )

    def set_stat(self, key: str, value: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO stats (key, value) VALUES (?, ?)", (key, value)
            )

    def get_stats(self) -> VMStats:
        rows = self._conn.execute("SELECT key, value FROM stats").fetchall()
        return stats_from_rows(rows)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

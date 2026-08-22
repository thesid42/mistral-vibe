from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from vibe.core.vibevm.models import ContextPage, PageState
from vibe.core.vibevm.store import PageStore

_NOW = "2026-01-01T00:00:00+00:00"


def _make_page(
    *,
    page_id: str = "P001",
    session_id: str = "sess-1",
    tool_call_id: str = "call-1",
    page_type: str = "read_file",
    content: str = "hello world",
    summary: str = "hello world",
    source_path: str | None = None,
    source_hash: str | None = None,
    token_count: int = 3,
    importance: float = 0.5,
    access_count: int = 0,
    created_seq: int = 0,
    last_accessed: str = _NOW,
    state: PageState = PageState.HOT,
) -> ContextPage:
    return ContextPage(
        id=page_id,
        session_id=session_id,
        tool_call_id=tool_call_id,
        page_type=page_type,
        content=content,
        summary=summary,
        source_path=source_path,
        source_hash=source_hash,
        token_count=token_count,
        importance=importance,
        access_count=access_count,
        created_seq=created_seq,
        created_at=_NOW,
        last_accessed=last_accessed,
        state=state,
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[PageStore]:
    page_store = PageStore(tmp_path / "session.db")
    yield page_store
    page_store.close()


def test_upsert_and_round_trip(store: PageStore) -> None:
    store.upsert_page(_make_page())

    fetched = store.get_by_tool_call_id("call-1")
    assert fetched is not None
    assert fetched.id == "P001"
    assert fetched.content == "hello world"
    assert fetched.state == PageState.HOT

    by_id = store.get_by_page_id("P001")
    assert by_id is not None
    assert by_id.tool_call_id == "call-1"


def test_get_missing_returns_none(store: PageStore) -> None:
    assert store.get_by_tool_call_id("nope") is None
    assert store.get_by_page_id("P999") is None


def test_upsert_by_tool_call_id_is_idempotent(store: PageStore) -> None:
    store.upsert_page(_make_page(content="version one", token_count=2))
    store.upsert_page(_make_page(content="version two", token_count=5, access_count=3))

    assert len(store.all_pages()) == 1
    fetched = store.get_by_tool_call_id("call-1")
    assert fetched is not None
    assert fetched.id == "P001"
    assert fetched.content == "version two"
    assert fetched.token_count == 5
    assert fetched.access_count == 3


def test_all_pages_ordered_by_created_seq(store: PageStore) -> None:
    store.upsert_page(_make_page(page_id="P002", tool_call_id="call-2", created_seq=5))
    store.upsert_page(_make_page(page_id="P001", tool_call_id="call-1", created_seq=1))
    store.upsert_page(_make_page(page_id="P003", tool_call_id="call-3", created_seq=9))

    ordered = store.all_pages()
    assert [p.id for p in ordered] == ["P001", "P002", "P003"]


def test_set_state(store: PageStore) -> None:
    store.upsert_page(_make_page())
    store.set_state("P001", PageState.COLD)

    updated = store.get_by_page_id("P001")
    assert updated is not None
    assert updated.state == PageState.COLD


def test_touch_increments_access_count_and_updates_last_accessed(
    store: PageStore,
) -> None:
    store.upsert_page(_make_page(last_accessed="2020-01-01T00:00:00+00:00"))

    store.touch("P001")
    first = store.get_by_page_id("P001")
    assert first is not None
    assert first.access_count == 1
    assert first.last_accessed != "2020-01-01T00:00:00+00:00"

    store.touch("P001")
    second = store.get_by_page_id("P001")
    assert second is not None
    assert second.access_count == 2


def test_next_page_id_increments(store: PageStore) -> None:
    assert store.next_page_id() == "P001"
    store.upsert_page(_make_page(page_id="P001"))
    assert store.next_page_id() == "P002"
    store.upsert_page(_make_page(page_id="P002", tool_call_id="call-2"))
    assert store.next_page_id() == "P003"


def test_bump_stat_and_get_stats(store: PageStore) -> None:
    store.bump_stat("evictions")
    store.bump_stat("evictions")
    store.bump_stat("tokens_evicted", by=150)

    stats = store.get_stats()
    assert stats.evictions == 2
    assert stats.tokens_evicted == 150
    assert stats.hits == 0
    assert stats.misses == 0


def test_search_fts_matches_content_and_summary(store: PageStore) -> None:
    assert store.fts_enabled
    store.upsert_page(
        _make_page(
            page_id="P001",
            tool_call_id="call-1",
            content="def parse_config(path): ...",
            summary="parse_config in config.py",
        )
    )
    store.upsert_page(
        _make_page(
            page_id="P002",
            tool_call_id="call-2",
            content="unrelated bash output",
            summary="ls -la output",
        )
    )

    results = store.search("parse_config")
    assert [p.id for p in results] == ["P001"]


def test_search_returns_empty_when_no_match(store: PageStore) -> None:
    store.upsert_page(_make_page())
    assert store.search("totally-absent-term") == []


def test_search_sanitizes_fts_operator_characters(store: PageStore) -> None:
    store.upsert_page(
        _make_page(content='has "quotes" and (parens) plus NOT operators', summary="s")
    )

    # Should not raise despite FTS5 operator characters/keywords in the query.
    results = store.search('"quotes" OR (parens) AND NOT')
    assert len(results) == 1


def test_search_like_fallback_when_fts_disabled(store: PageStore) -> None:
    store.fts_enabled = False
    store.upsert_page(
        _make_page(
            page_id="P001",
            tool_call_id="call-1",
            content="Alpha Bravo",
            summary="greek letters",
        )
    )
    store.upsert_page(
        _make_page(
            page_id="P002",
            tool_call_id="call-2",
            content="charlie delta",
            summary="more letters",
        )
    )

    # Case-insensitive match against content.
    assert [p.id for p in store.search("alpha")] == ["P001"]
    # Case-insensitive match against summary.
    assert [p.id for p in store.search("GREEK")] == ["P001"]


def test_search_like_ranks_by_term_hit_count(store: PageStore) -> None:
    store.fts_enabled = False
    store.upsert_page(
        _make_page(
            page_id="P001",
            tool_call_id="call-1",
            content="alpha alpha alpha",
            summary="s",
        )
    )
    store.upsert_page(
        _make_page(page_id="P002", tool_call_id="call-2", content="alpha", summary="s")
    )

    results = store.search("alpha")
    assert [p.id for p in results] == ["P001", "P002"]


def test_search_like_respects_limit(store: PageStore) -> None:
    store.fts_enabled = False
    for i in range(10):
        store.upsert_page(
            _make_page(
                page_id=f"P{i:03d}",
                tool_call_id=f"call-{i}",
                content="needle",
                summary="s",
            )
        )

    assert len(store.search("needle", limit=3)) == 3


def _log_mentioning_terms() -> str:
    """A large log whose lines repeatedly mention auth.py/refresh/check --
    enough raw term frequency to outrank a small real auth.py page on bm25
    and on LIKE hit-count alike, unless filename-aware ranking kicks in.
    """
    lines = [
        f"INFO line {i:04d}: routine heartbeat, nothing unusual here, server ok"
        for i in range(1500)
    ]
    for i in range(0, 1500, 3):
        lines[i] = f"DEBUG auth.py:{i} refresh_session check exp iat cycle complete"
    return "\n".join(lines)


def _upsert_log_and_auth_pages(store: PageStore) -> None:
    store.upsert_page(
        _make_page(
            page_id="P001",
            tool_call_id="call-1",
            content=_log_mentioning_terms(),
            summary="server log excerpt (routine heartbeat records)",
            source_path="/var/log/server.log",
        )
    )
    store.upsert_page(
        _make_page(
            page_id="P002",
            tool_call_id="call-2",
            content="def refresh_session():\n    check_exp_iat()\n    return True\n",
            summary="refresh_session helper in auth.py",
            source_path="/repo/auth.py",
        )
    )


def test_search_fts_prioritizes_filename_match_over_term_frequency(
    store: PageStore,
) -> None:
    assert store.fts_enabled
    _upsert_log_and_auth_pages(store)

    results = store.search("auth.py refresh check")

    assert results[0].id == "P002"


def test_search_like_prioritizes_filename_match_over_term_frequency(
    store: PageStore,
) -> None:
    store.fts_enabled = False
    _upsert_log_and_auth_pages(store)

    results = store.search("auth.py refresh check")

    assert results[0].id == "P002"


def test_search_short_filename_terms_leave_ranking_unchanged(store: PageStore) -> None:
    store.fts_enabled = False
    store.upsert_page(
        _make_page(
            page_id="P001", tool_call_id="call-1", content="ok ok ok", summary="s"
        )
    )
    store.upsert_page(
        _make_page(
            page_id="P002",
            tool_call_id="call-2",
            content="ok",
            summary="s",
            source_path="/repo/ok.py",  # "ok" is only 2 chars: too short to boost
        )
    )

    results = store.search("ok")

    assert [p.id for p in results] == [
        "P001",
        "P002",
    ]  # unchanged: pure hit-count order


def test_search_like_respects_limit_after_filename_partition(store: PageStore) -> None:
    store.fts_enabled = False
    for i in range(3):
        store.upsert_page(
            _make_page(
                page_id=f"P00{i}",
                tool_call_id=f"call-{i}",
                content="needle " * 10,
                summary="s",
            )
        )
    for i in range(3, 5):
        store.upsert_page(
            _make_page(
                page_id=f"P00{i}",
                tool_call_id=f"call-{i}",
                content="needle",
                summary="s",
                source_path=f"/repo/needle{i}.py",
            )
        )

    results = store.search("needle check", limit=2)

    assert len(results) == 2
    assert {p.id for p in results} == {"P003", "P004"}

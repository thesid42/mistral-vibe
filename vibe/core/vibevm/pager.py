from __future__ import annotations

import bisect
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
from typing import TYPE_CHECKING, Protocol

from vibe.core.paths import VM_DIR
from vibe.core.types import LLMMessage, Role
from vibe.core.utils.tokens import approx_token_count, truncate_middle_to_tokens
from vibe.core.vibevm.models import ContextPage, PageState, RecallOutcome, VMSnapshot
from vibe.core.vibevm.store import (
    PageStore,
    filename_match_terms,
    page_matches_filename,
)

if TYPE_CHECKING:
    from vibe.core.config import VibeConfigSchema

STUB_PREFIX = "[vibevm:paged-out]"


class _VibeVMConfigLike(Protocol):
    """Structural shape of ``VibeConfigSchema.vibevm`` (§2 of the design).

    Kept as a local ``Protocol`` rather than importing ``VibeVMConfig`` so this
    package has zero hard dependency on the config package's own edits.
    """

    enabled: bool
    context_budget: int
    evict_target_ratio: float
    min_page_tokens: int
    protect_recent_turns: int


_PER_MESSAGE_OVERHEAD = 8
_SUMMARY_MAX_TOKENS = 30  # ~120 chars via truncate_middle_to_tokens's 4 bytes/token
_STALE_TEXT_MAX_TOKENS = 4000
_STALE_PROBE_LIMIT = 5  # cap staleness probing to the top N results, bounds file IO
_COHERENCE_PROBE_LIMIT = 8  # cap coherence probing per apply(), bounds file IO
_PAGE_TABLE_PREFIX = "[vibevm:page-table]"
_PAGE_TABLE_MAX_ENTRIES = 20
_PAGE_TABLE_MAX_TOKENS = 600
_DEFAULT_IMPORTANCE = 0.5
_EXCERPT_MAX_TOKENS = 1500  # recall() windows content larger than this
_FULL_MAX_TOKENS = 6000  # recall(full=True) still caps at this many tokens
_EXCERPT_CONTEXT_LINES = 3  # lines of context kept on each side of a match
_EXCERPT_MAX_WINDOWS = 5
_MIN_ASSISTANT_MSGS_AFTER = 2  # tier-2 age gate: turns since a page was created
_PROTECT_LAST_TOOLS = 4  # tier-2 positional gate: never the newest N tool results
_THOUSAND = 1000
_IMPORTANCE_WEIGHTS: dict[str, float] = {
    "read_file": 0.7,
    "edit": 0.7,
    "write_file": 0.7,
    "grep": 0.45,
    "bash": 0.55,
    "web_fetch": 0.35,
    "web_search": 0.35,
    # Recalled content is the model's explicitly demanded working set; evicting
    # it again invites a recall/evict thrash loop, so it goes last.
    "recall_context": 0.85,
}


def make_stub(page: ContextPage) -> str:
    source_part = f" source={page.source_path}" if page.source_path else ""
    return (
        f"{STUB_PREFIX} page={page.id} type={page.page_type} "
        f"tokens~{page.token_count}{source_part}\n"
        f"summary: {page.summary}\n"
        f'Full content is preserved. Call recall_context(query=..., page_id="{page.id}") '
        "to restore it.\n"
        "Do not re-read, grep, or re-run the original tool for this content — "
        "use recall_context instead."
    )


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _summarize(content: str, source_path: str | None) -> str:
    first_line = next((ln.strip() for ln in content.splitlines() if ln.strip()), "")
    summary = truncate_middle_to_tokens(first_line, _SUMMARY_MAX_TOKENS)
    if not source_path:
        return summary
    return f"{summary} ({source_path})" if summary else source_path


def _extract_source_path(
    messages: Sequence[LLMMessage], tool_call_id: str
) -> str | None:
    for message in messages:
        for call in message.tool_calls or []:
            if call.id != tool_call_id:
                continue
            try:
                args = json.loads(call.function.arguments or "")
            except json.JSONDecodeError:
                return None
            file_path = args.get("file_path") if isinstance(args, dict) else None
            return file_path if isinstance(file_path, str) else None
    return None


def _source_hash(source_path: str | None) -> str | None:
    if not source_path:
        return None
    try:
        data = Path(source_path).read_bytes()
    except OSError:
        return None
    return hashlib.sha256(data).hexdigest()


def _estimate_tokens(messages: Sequence[LLMMessage]) -> int:
    total = 0
    for message in messages:
        total += approx_token_count(message.content or "") + _PER_MESSAGE_OVERHEAD
        for call in message.tool_calls or []:
            total += approx_token_count(call.function.arguments or "")
    return total


def _protected_boundary_index(
    messages: Sequence[LLMMessage], protect_recent_turns: int
) -> int:
    if protect_recent_turns <= 0:
        return len(messages)
    user_indices = [
        index
        for index, message in enumerate(messages)
        if message.role == Role.user and not message.injected
    ]
    if not user_indices:
        return len(messages)
    cut = max(0, len(user_indices) - protect_recent_turns)
    return user_indices[cut]


def _score(page: ContextPage, max_seq: int) -> float:
    recency = page.created_seq / max_seq
    frequency = min(page.access_count, 5) / 5
    return 0.45 * recency + 0.25 * frequency + 0.30 * page.importance


def _tier1_candidates(
    pages_by_tcid: dict[str, ContextPage], evicted: set[str], boundary: int
) -> list[ContextPage]:
    """Round-protected candidates: HOT, unevicted, outside the last N user rounds."""
    return [
        p
        for p in pages_by_tcid.values()
        if p.state == PageState.HOT and p.id not in evicted and p.created_seq < boundary
    ]


def _last_tool_call_ids(messages: Sequence[LLMMessage], count: int) -> set[str]:
    """The tool_call_ids of the last ``count`` tool-role messages, by view position."""
    ids = [m.tool_call_id for m in messages if m.role == Role.tool and m.tool_call_id]
    return set(ids[-count:]) if count > 0 else set()


def _assistant_messages_after(assistant_indices: list[int], created_seq: int) -> int:
    return len(assistant_indices) - bisect.bisect_right(assistant_indices, created_seq)


def _tier2_candidates(
    pages_by_tcid: dict[str, ContextPage],
    evicted: set[str],
    assistant_indices: list[int],
    protected_recent_tools: set[str],
) -> list[ContextPage]:
    """Age-based fallback for when round protection leaves nothing evictable.

    A page the model has answered past twice (``_MIN_ASSISTANT_MSGS_AFTER``
    assistant turns have followed it) is digested into its own visible
    messages by now; evicting it mid-turn is safe, and windowed page faults
    make any re-fault cheap. The last ``_PROTECT_LAST_TOOLS`` tool results by
    view position are exempt regardless of age, so the model's immediate
    working set is never touched.
    """
    return [
        p
        for p in pages_by_tcid.values()
        if p.state == PageState.HOT
        and p.id not in evicted
        and p.tool_call_id not in protected_recent_tools
        and _assistant_messages_after(assistant_indices, p.created_seq)
        >= _MIN_ASSISTANT_MSGS_AFTER
    ]


def _query_terms(query: str) -> list[str]:
    """Lowercase word tokens -- same tokenization idiom ``store.search`` uses."""
    return [t.lower() for t in re.findall(r"\w+", query)]


def _format_k_tokens(tokens: int) -> str:
    if tokens < _THOUSAND:
        return str(tokens)
    value = f"{tokens / _THOUSAND:.1f}"
    if value.endswith(".0"):
        value = value[:-2]
    return f"{value}k"


def _windows_around(matched: list[int], line_count: int) -> list[tuple[int, int]]:
    return [
        (
            max(0, i - _EXCERPT_CONTEXT_LINES),
            min(line_count, i + _EXCERPT_CONTEXT_LINES + 1),
        )
        for i in matched
    ]


def _merge_windows(windows: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(windows):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _assemble_excerpt(lines: list[str], windows: list[tuple[int, int]]) -> str:
    # Whole-line granularity only: stop the instant a line wouldn't fit rather
    # than trim inside it, so every returned line stays verbatim.
    max_chars = _EXCERPT_MAX_TOKENS * 4  # matches approx_token_count's ratio
    parts: list[str] = []
    total_chars = 0
    prev_end = 0
    for start, end in windows:
        if parts and start > prev_end:
            marker = f"... [{start - prev_end} lines skipped] ..."
            if total_chars + len(marker) > max_chars:
                return "\n".join(parts)
            parts.append(marker)
            total_chars += len(marker) + 1
        for line in lines[start:end]:
            if total_chars + len(line) > max_chars:
                return "\n".join(parts)
            parts.append(line)
            total_chars += len(line) + 1
        prev_end = end
    return "\n".join(parts)


def _excerpt(content: str, query: str) -> str | None:
    """Focused windows around query-term matches, or None if nothing matched."""
    terms = _query_terms(query)
    if not terms:
        return None
    lines = content.splitlines()
    matched = [
        i for i, line in enumerate(lines) if any(term in line.lower() for term in terms)
    ]
    if not matched:
        return None
    windows = _merge_windows(
        _windows_around(matched[:_EXCERPT_MAX_WINDOWS], len(lines))
    )
    return _assemble_excerpt(lines, windows)


def _windowed_content(
    content: str, query: str, *, full: bool, already_hot: bool
) -> tuple[str, str | None]:
    if already_hot:
        # Content the model never lost (page stayed HOT) is already fully
        # present in its context; re-showing it in full would double its cost,
        # so this ignores both size and ``full`` and always excerpts.
        excerpt = _excerpt(content, query) or truncate_middle_to_tokens(
            content, _EXCERPT_MAX_TOKENS
        )
        return (
            excerpt,
            "This page is already present in your context in full; excerpt shown.",
        )

    original_tokens = approx_token_count(content)
    if full:
        if original_tokens <= _FULL_MAX_TOKENS:
            return content, None
        note = (
            f"Showing ~{_format_k_tokens(_FULL_MAX_TOKENS)} of "
            f"~{_format_k_tokens(original_tokens)} tokens (middle omitted); "
            "use a specific query to excerpt the exact region."
        )
        return truncate_middle_to_tokens(content, _FULL_MAX_TOKENS), note

    if original_tokens <= _EXCERPT_MAX_TOKENS:
        return content, None
    excerpt = _excerpt(content, query) or truncate_middle_to_tokens(
        content, _EXCERPT_MAX_TOKENS
    )
    note = (
        f"Excerpt of a ~{_format_k_tokens(original_tokens)}-token page; "
        "pass full=true for the complete content."
    )
    return excerpt, note


def _read_source_file(source_path: str) -> tuple[bytes, str] | None:
    """Current bytes and sha256 hex digest of ``source_path``.

    None if the file is missing or unreadable.
    """
    path = Path(source_path)
    try:
        if not path.exists():
            return None
        data = path.read_bytes()
    except OSError:
        return None
    return data, hashlib.sha256(data).hexdigest()


def _is_stale_and_filename_matched(
    page: ContextPage, terms: list[str], cache: dict[str, tuple[bytes, str] | None]
) -> bool:
    """True if ``page`` matches ``terms`` by filename and its file changed on disk.

    ``cache`` memoizes reads by path so candidates sharing a source file (e.g.
    duplicate pages for the same file) only hit disk once.
    """
    if not page.source_path or not page.source_hash:
        return False
    if not page_matches_filename(page, terms):
        return False
    if page.source_path not in cache:
        cache[page.source_path] = _read_source_file(page.source_path)
    read = cache[page.source_path]
    return read is not None and read[1] != page.source_hash


def _promote_stale_filename_match(
    results: list[ContextPage], query: str
) -> list[ContextPage]:
    """Move the highest-ranked stale, filename-matching page to the front.

    Guards against the ranking bug where a large unrelated page (e.g. a log)
    outranks the actual file the query is asking about: only a page whose
    filename matches the query's own terms is eligible, so an unrelated stale
    page never gets promoted. Displaced pages keep their relative order.
    """
    terms = filename_match_terms(query)
    cache: dict[str, tuple[bytes, str] | None] = {}
    for page in results[:_STALE_PROBE_LIMIT]:
        if not _is_stale_and_filename_matched(page, terms, cache):
            continue
        if page.id == results[0].id:
            return results
        return [page, *(p for p in results if p.id != page.id)]
    return results


def _invalidate_changed_hot_pages(store: PageStore, pages: list[ContextPage]) -> None:
    """Flip HOT file-backed pages COLD when their source file changed on disk.

    A HOT page is replayed verbatim into the view on every apply() -- unlike
    a COLD page, it's never looked up through recall(), so nothing else ever
    re-checks it against disk. Left alone, the model keeps reading a stale
    snapshot forever. Probes the _COHERENCE_PROBE_LIMIT most recently created
    file-backed HOT pages per call; a missing/unreadable file leaves the page
    HOT (legitimate memory of a since-deleted file), and an unchanged file
    causes no store writes at all.
    """
    candidate_indices = [
        i
        for i, p in enumerate(pages)
        if p.state == PageState.HOT and p.source_path and p.source_hash
    ]
    candidate_indices.sort(key=lambda i: pages[i].created_seq, reverse=True)
    cache: dict[str, tuple[bytes, str] | None] = {}
    for i in candidate_indices[:_COHERENCE_PROBE_LIMIT]:
        page = pages[i]
        source_path = page.source_path
        if source_path is None:
            continue
        if source_path not in cache:
            cache[source_path] = _read_source_file(source_path)
        read = cache[source_path]
        if read is None or read[1] == page.source_hash:
            continue
        store.set_state(page.id, PageState.COLD)
        store.bump_stat("evictions")
        store.bump_stat("tokens_evicted", by=page.token_count)
        pages[i] = page.model_copy(update={"state": PageState.COLD})


def _page_table_line(page: ContextPage, snapshots: int) -> str:
    source = f" source={Path(page.source_path).name}" if page.source_path else ""
    suffix = f" (x{snapshots} snapshots)" if snapshots > 1 else ""
    return (
        f"{page.id} {page.page_type} ~{_format_k_tokens(page.token_count)}"
        f"{source} — {page.summary}{suffix}"
    )


def _inject_page_table(out: list[LLMMessage], pages: list[ContextPage]) -> None:
    """Append a catalog of orphaned pages to the newest compaction envelope.

    Compaction cuts pre-boundary messages from the view — including the
    ``[vibevm:paged-out]`` stubs — so the model loses its map of what the page
    store can restore and falls back to re-reading files. This re-surfaces
    that map as a few-hundred-token index on the envelope itself, in the view
    only; history is never touched.
    """
    boundary_i = next(
        (
            i
            for i in range(len(out) - 1, -1, -1)
            if out[i].context_boundary == "compaction"
        ),
        None,
    )
    if boundary_i is None:
        return
    visible = {m.tool_call_id for m in out if m.role == Role.tool and m.tool_call_id}
    orphans = sorted(
        (p for p in pages if p.tool_call_id not in visible),
        key=lambda p: p.created_seq,
        reverse=True,
    )
    if not orphans:
        return

    snapshot_counts: dict[str, int] = {}
    deduped: list[ContextPage] = []
    for page in orphans:
        if page.source_path:
            if page.source_path in snapshot_counts:
                snapshot_counts[page.source_path] += 1
                continue
            snapshot_counts[page.source_path] = 1
        deduped.append(page)

    lines = [
        f"{_PAGE_TABLE_PREFIX} These pages from before the compaction are "
        "preserved in the VibeVM store and are NOT shown above. Restore any "
        'with recall_context(page_id="...") or a keyword query:'
    ]
    shown = 0
    for page in deduped[:_PAGE_TABLE_MAX_ENTRIES]:
        snapshots = snapshot_counts.get(page.source_path or "", 1)
        line = _page_table_line(page, snapshots)
        if approx_token_count("\n".join([*lines, line])) > _PAGE_TABLE_MAX_TOKENS:
            break
        lines.append(line)
        shown += 1
    if shown < len(deduped):
        lines.append(
            f"...and {len(deduped) - shown} more — search with "
            "recall_context(query=...)."
        )

    envelope = out[boundary_i]
    block = "\n".join(lines)
    out[boundary_i] = envelope.model_copy(
        update={"content": f"{envelope.content or ''}\n\n{block}"}
    )


class VibeVM:
    """Per-session view-transform manager: pages large tool results out of the
    outgoing message array under budget pressure and restores them on demand.

    History is never mutated — ``apply`` only ever builds a new list, passing
    untouched messages through by reference and replacing paged-out tool
    messages with a fresh ``model_copy``.
    """

    def __init__(
        self, *, session_id: str, config_getter: Callable[[], VibeConfigSchema]
    ) -> None:
        self.session_id = session_id
        self._config_getter = config_getter
        self._store: PageStore | None = None
        self._last_written_budget: int | None = None

    @property
    def enabled(self) -> bool:
        cfg = getattr(self._config_getter(), "vibevm", None)
        return bool(cfg is not None and cfg.enabled)

    def _db_path(self) -> Path:
        return VM_DIR.path / f"{self.session_id}.db"

    def _ensure_store(self) -> PageStore:
        if self._store is None:
            self._store = PageStore(self._db_path())
        return self._store

    def apply(self, messages: Sequence[LLMMessage]) -> list[LLMMessage]:
        cfg: _VibeVMConfigLike | None = getattr(self._config_getter(), "vibevm", None)
        if cfg is None or not cfg.enabled:
            return list(messages)

        store = self._ensure_store()
        if cfg.context_budget != self._last_written_budget:
            store.set_stat("context_budget", cfg.context_budget)
            self._last_written_budget = cfg.context_budget
        self._register_new_pages(store, messages, cfg.min_page_tokens)

        pages = store.all_pages()
        _invalidate_changed_hot_pages(store, pages)
        pages_by_tcid = {p.tool_call_id: p for p in pages}
        out, index_by_tcid = self._substitute(messages, pages_by_tcid)
        _inject_page_table(out, pages)
        self._evict_to_budget(store, messages, out, index_by_tcid, pages_by_tcid, cfg)
        return out

    def recall(
        self, query: str, page_id: str | None = None, full: bool = False
    ) -> RecallOutcome:
        store = self._ensure_store()
        results = self._lookup(store, query, page_id)
        if not results:
            store.bump_stat("misses")
            return RecallOutcome(status="miss")
        if page_id is None:
            results = _promote_stale_filename_match(results, query)

        top, *rest = results
        was_cold = top.state == PageState.COLD
        if was_cold:
            store.bump_stat("page_faults")
        store.bump_stat("hits")
        store.touch(top.id)
        top = store.get_by_page_id(top.id) or top

        stale = self._refresh_if_stale(store, top)
        if stale is not None:
            top, note = stale
            status = "stale_refreshed"
        else:
            note = None
            status = "ok" if was_cold else "already_hot"

        content, excerpt_note = _windowed_content(
            top.content, query, full=full, already_hot=status == "already_hot"
        )
        if excerpt_note is not None:
            note = f"{note} {excerpt_note}" if note else excerpt_note

        return RecallOutcome(
            status=status,
            page_id=top.id,
            page_type=top.page_type,
            source_path=top.source_path,
            content=content,
            note=note,
            other_matches=[f"{p.id} — {p.summary}" for p in rest[:4]],
        )

    def snapshot(self) -> VMSnapshot:
        store = self._ensure_store()
        pages = [p.model_copy(update={"content": ""}) for p in store.all_pages()]
        return VMSnapshot(
            pages=pages, stats=store.get_stats(), db_path=str(self._db_path())
        )

    def rebind(self, session_id: str) -> None:
        if session_id == self.session_id:
            return
        if self._store is not None:
            self._store.close()
            self._store = None
        self._last_written_budget = None
        self.session_id = session_id

    def _lookup(
        self, store: PageStore, query: str, page_id: str | None
    ) -> list[ContextPage]:
        if page_id is not None:
            page = store.get_by_page_id(page_id)
            return [page] if page is not None else []
        return store.search(query)

    def _refresh_if_stale(
        self, store: PageStore, page: ContextPage
    ) -> tuple[ContextPage, str] | None:
        if not page.source_path or not page.source_hash:
            return None
        read = _read_source_file(page.source_path)
        if read is None:
            return None
        current_bytes, current_hash = read
        if current_hash == page.source_hash:
            return None

        fresh_text = truncate_middle_to_tokens(
            current_bytes.decode("utf-8", errors="replace"), _STALE_TEXT_MAX_TOKENS
        )
        refreshed = page.model_copy(
            update={
                "content": fresh_text,
                "source_hash": current_hash,
                "summary": _summarize(fresh_text, page.source_path),
                "token_count": approx_token_count(fresh_text),
            }
        )
        store.upsert_page(refreshed)
        store.bump_stat("stale_recalls")
        note = (
            f"Source file changed on disk (was {page.source_hash[:7]}, "
            f"now {current_hash[:7]}); showing current content."
        )
        return refreshed, note

    def _register_new_pages(
        self, store: PageStore, messages: Sequence[LLMMessage], min_page_tokens: int
    ) -> None:
        min_chars = min_page_tokens * 4
        for index, message in enumerate(messages):
            if message.role != Role.tool or not message.tool_call_id:
                continue
            content = message.content or ""
            if len(content) < min_chars or content.startswith(STUB_PREFIX):
                continue
            if store.get_by_tool_call_id(message.tool_call_id) is not None:
                continue
            self._register_page(store, messages, index, message, content)

    def _register_page(
        self,
        store: PageStore,
        messages: Sequence[LLMMessage],
        index: int,
        message: LLMMessage,
        content: str,
    ) -> None:
        page_type = message.name or ""
        source_path = (
            _extract_source_path(messages, message.tool_call_id or "")
            if page_type == "read_file"
            else None
        )
        now = _utc_now_iso()
        page = ContextPage(
            id=store.next_page_id(),
            session_id=self.session_id,
            tool_call_id=message.tool_call_id or "",
            page_type=page_type,
            content=content,
            summary=_summarize(content, source_path),
            source_path=source_path,
            source_hash=_source_hash(source_path),
            token_count=approx_token_count(content),
            importance=_IMPORTANCE_WEIGHTS.get(page_type, _DEFAULT_IMPORTANCE),
            created_seq=index,
            created_at=now,
            last_accessed=now,
            state=PageState.HOT,
        )
        store.upsert_page(page)

    def _substitute(
        self, messages: Sequence[LLMMessage], pages_by_tcid: dict[str, ContextPage]
    ) -> tuple[list[LLMMessage], dict[str, int]]:
        out: list[LLMMessage] = []
        index_by_tcid: dict[str, int] = {}
        for message in messages:
            page = pages_by_tcid.get(message.tool_call_id or "")
            if (
                message.role == Role.tool
                and page is not None
                and page.state == PageState.COLD
            ):
                out.append(message.model_copy(update={"content": make_stub(page)}))
            else:
                out.append(message)
            if message.role == Role.tool and message.tool_call_id:
                index_by_tcid[message.tool_call_id] = len(out) - 1
        return out, index_by_tcid

    def _evict_to_budget(
        self,
        store: PageStore,
        messages: Sequence[LLMMessage],
        out: list[LLMMessage],
        index_by_tcid: dict[str, int],
        pages_by_tcid: dict[str, ContextPage],
        cfg: _VibeVMConfigLike,
    ) -> None:
        budget = cfg.context_budget
        estimate = _estimate_tokens(out)
        if estimate <= budget:
            return

        target = budget * cfg.evict_target_ratio
        boundary = _protected_boundary_index(messages, cfg.protect_recent_turns)
        assistant_indices = [
            i for i, m in enumerate(messages) if m.role == Role.assistant
        ]
        protected_recent_tools = _last_tool_call_ids(messages, _PROTECT_LAST_TOOLS)
        max_seq = max((p.created_seq for p in pages_by_tcid.values()), default=0) or 1
        evicted: set[str] = set()

        while estimate > target:
            candidates = _tier1_candidates(pages_by_tcid, evicted, boundary)
            if not candidates:
                if estimate <= budget:
                    return
                candidates = _tier2_candidates(
                    pages_by_tcid, evicted, assistant_indices, protected_recent_tools
                )
                if not candidates:
                    return
            victim = min(candidates, key=lambda p: _score(p, max_seq))
            evicted.add(victim.id)
            store.set_state(victim.id, PageState.COLD)
            store.bump_stat("evictions")
            store.bump_stat("tokens_evicted", by=victim.token_count)
            idx = index_by_tcid.get(victim.tool_call_id)
            if idx is not None:
                out[idx] = out[idx].model_copy(update={"content": make_stub(victim)})
            estimate = _estimate_tokens(out)

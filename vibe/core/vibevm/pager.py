from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from vibe.core.paths import VM_DIR
from vibe.core.types import LLMMessage, Role
from vibe.core.utils.tokens import approx_token_count, truncate_middle_to_tokens
from vibe.core.vibevm.models import ContextPage, PageState, RecallOutcome, VMSnapshot
from vibe.core.vibevm.store import PageStore

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
_DEFAULT_IMPORTANCE = 0.5
_PROTECT_LAST_TOOLS = 3  # positional fallback: never evict the newest N tool results
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
        "to restore it."
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


def _last_tool_call_ids(messages: Sequence[LLMMessage], count: int) -> set[str]:
    """The tool_call_ids of the last ``count`` tool-role messages, by view position."""
    ids = [m.tool_call_id for m in messages if m.role == Role.tool and m.tool_call_id]
    return set(ids[-count:]) if count > 0 else set()


def _tier1_candidates(
    pages_by_tcid: dict[str, ContextPage], evicted: set[str], boundary: int
) -> list[ContextPage]:
    """Round-protected candidates: HOT, unevicted, outside the last N user rounds."""
    return [
        p
        for p in pages_by_tcid.values()
        if p.state == PageState.HOT and p.id not in evicted and p.created_seq < boundary
    ]


def _tier2_candidates(
    pages_by_tcid: dict[str, ContextPage], evicted: set[str], protected_tcids: set[str]
) -> list[ContextPage]:
    """Positional fallback for when round protection leaves nothing evictable.

    HOT, unevicted, and not one of the last ``_PROTECT_LAST_TOOLS`` tool
    messages by view position — so a single huge turn (one round, many big
    tool results) can still shed content once truly over the hard budget.
    """
    return [
        p
        for p in pages_by_tcid.values()
        if p.state == PageState.HOT
        and p.id not in evicted
        and p.tool_call_id not in protected_tcids
    ]


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

        pages_by_tcid = {p.tool_call_id: p for p in store.all_pages()}
        out, index_by_tcid = self._substitute(messages, pages_by_tcid)
        self._evict_to_budget(store, messages, out, index_by_tcid, pages_by_tcid, cfg)
        return out

    def recall(self, query: str, page_id: str | None = None) -> RecallOutcome:
        store = self._ensure_store()
        results = self._lookup(store, query, page_id)
        if not results:
            store.bump_stat("misses")
            return RecallOutcome(status="miss")

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

        return RecallOutcome(
            status=status,
            page_id=top.id,
            page_type=top.page_type,
            source_path=top.source_path,
            content=top.content,
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
        path = Path(page.source_path)
        try:
            if not path.exists():
                return None
            current_bytes = path.read_bytes()
        except OSError:
            return None
        current_hash = hashlib.sha256(current_bytes).hexdigest()
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
        protected_recent_tools = _last_tool_call_ids(messages, _PROTECT_LAST_TOOLS)
        max_seq = max((p.created_seq for p in pages_by_tcid.values()), default=0) or 1
        evicted: set[str] = set()

        while estimate > target:
            candidates = _tier1_candidates(pages_by_tcid, evicted, boundary)
            if not candidates:
                if estimate <= budget:
                    return
                candidates = _tier2_candidates(
                    pages_by_tcid, evicted, protected_recent_tools
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

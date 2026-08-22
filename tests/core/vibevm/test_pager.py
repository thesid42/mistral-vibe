from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock

from pydantic import BaseModel

from vibe.core.types import FunctionCall, LLMMessage, Role, ToolCall
from vibe.core.vibevm.models import ContextPage, PageState
from vibe.core.vibevm.pager import STUB_PREFIX, VibeVM, _estimate_tokens, make_stub


class _FakeVibeVMConfig(BaseModel):
    enabled: bool = True
    context_budget: int = 100_000
    evict_target_ratio: float = 0.8
    min_page_tokens: int = 50
    protect_recent_turns: int = 1


class _FakeConfig(BaseModel):
    vibevm: _FakeVibeVMConfig | None = None


class _ConfigWithoutVibeVM(BaseModel):
    """Shape of the config before E3's edit lands: no ``vibevm`` attribute at all."""


def _config(
    *,
    enabled: bool = True,
    context_budget: int = 100_000,
    evict_target_ratio: float = 0.8,
    min_page_tokens: int = 50,
    protect_recent_turns: int = 1,
) -> _FakeConfig:
    return _FakeConfig(
        vibevm=_FakeVibeVMConfig(
            enabled=enabled,
            context_budget=context_budget,
            evict_target_ratio=evict_target_ratio,
            min_page_tokens=min_page_tokens,
            protect_recent_turns=protect_recent_turns,
        )
    )


def _user(content: str, *, injected: bool = False) -> LLMMessage:
    return LLMMessage(role=Role.user, content=content, injected=injected)


def _assistant_call(tool_call_id: str, name: str, arguments: str = "{}") -> LLMMessage:
    return LLMMessage(
        role=Role.assistant,
        content="",
        tool_calls=[
            ToolCall(
                id=tool_call_id, function=FunctionCall(name=name, arguments=arguments)
            )
        ],
    )


def _tool_result(tool_call_id: str, name: str, content: str) -> LLMMessage:
    return LLMMessage(
        role=Role.tool, tool_call_id=tool_call_id, name=name, content=content
    )


def _log_content(total_lines: int, distinctive: dict[int, str]) -> str:
    body = [
        f"INFO line {i:04d}: routine heartbeat, nothing unusual here"
        for i in range(total_lines)
    ]
    for index, text in distinctive.items():
        body[index] = text
    return "\n".join(body)


def test_make_stub_directs_model_to_recall_not_reread() -> None:
    page = ContextPage(
        id="P042",
        session_id="s",
        tool_call_id="tc",
        page_type="read_file",
        content="x" * 100,
        summary="server.log excerpt",
        source_path="/tmp/server.log",
        token_count=100,
        created_seq=1,
        created_at="2026-01-01T00:00:00+00:00",
        last_accessed="2026-01-01T00:00:00+00:00",
        state=PageState.COLD,
    )
    stub = make_stub(page)
    assert stub.startswith(STUB_PREFIX)
    assert 'page_id="P042"' in stub
    assert "Do not re-read, grep, or re-run" in stub
    assert "use recall_context instead" in stub


def test_disabled_returns_passthrough_identical_list() -> None:
    cfg = _config(enabled=False)
    vm = VibeVM(session_id="disabled-1", config_getter=lambda: cfg)
    messages = [_user("hello"), LLMMessage(role=Role.assistant, content="hi")]

    out = vm.apply(messages)

    assert [m.content for m in out] == [m.content for m in messages]
    assert all(a is b for a, b in zip(out, messages, strict=True))
    assert vm._store is None  # disabled: never touches disk


def test_missing_vibevm_config_disables_safely() -> None:
    vm = VibeVM(session_id="no-config-1", config_getter=lambda: _ConfigWithoutVibeVM())
    messages = [_user("hello")]

    out = vm.apply(messages)

    assert out == messages
    assert vm._store is None
    assert vm.enabled is False


def test_enabled_property_reflects_config() -> None:
    on = _config(enabled=True)
    off = _config(enabled=False)
    assert VibeVM(session_id="e-on", config_getter=lambda: on).enabled is True
    assert VibeVM(session_id="e-off", config_getter=lambda: off).enabled is False


def test_registration_threshold(config_dir: Path) -> None:
    cfg = _config(min_page_tokens=50)  # 200-char threshold
    vm = VibeVM(session_id="reg-1", config_getter=lambda: cfg)
    messages = [
        _user("go"),
        _assistant_call("call-big", "bash"),
        _tool_result("call-big", "bash", "y" * 250),
        _assistant_call("call-small", "bash"),
        _tool_result("call-small", "bash", "small output"),
    ]

    vm.apply(messages)

    pages_by_tcid = {p.tool_call_id: p for p in vm.snapshot().pages}
    assert "call-big" in pages_by_tcid
    assert "call-small" not in pages_by_tcid


def test_stub_like_content_is_not_reregistered(config_dir: Path) -> None:
    cfg = _config(min_page_tokens=1)
    vm = VibeVM(session_id="reg-2", config_getter=lambda: cfg)
    stub_content = f"{STUB_PREFIX} page=P099 type=bash tokens~10\nsummary: x\nrestore."
    messages = [
        _user("go"),
        _assistant_call("call-1", "bash"),
        _tool_result("call-1", "bash", stub_content),
    ]

    vm.apply(messages)

    assert vm.snapshot().pages == []


def test_apply_never_mutates_input_and_passes_untouched_by_reference(
    config_dir: Path,
) -> None:
    cfg = _config(min_page_tokens=1, context_budget=100_000)  # nothing evicted
    vm = VibeVM(session_id="purity-1", config_getter=lambda: cfg)
    tool_msg = _tool_result("call-1", "bash", "z" * 100)
    messages = [_user("go"), _assistant_call("call-1", "bash"), tool_msg]
    original_contents = [m.content for m in messages]

    out = vm.apply(messages)

    assert [m.content for m in messages] == original_contents
    assert out[0] is messages[0]
    assert out[1] is messages[1]
    assert out[2] is messages[2]  # HOT tool message: untouched, passed by reference


def test_read_file_page_captures_source_path_and_hash(
    config_dir: Path, tmp_path: Path
) -> None:
    target = tmp_path / "example.py"
    target.write_text("print('hi')\n" * 50, encoding="utf-8")
    cfg = _config(min_page_tokens=1)
    vm = VibeVM(session_id="srcfile-1", config_getter=lambda: cfg)
    args = json.dumps({"file_path": str(target)})
    messages = [
        _user("go"),
        _assistant_call("call-1", "read_file", args),
        _tool_result("call-1", "read_file", "print('hi')\n" * 50),
    ]

    vm.apply(messages)

    page = next(p for p in vm.snapshot().pages if p.tool_call_id == "call-1")
    assert page.source_path == str(target)
    assert page.source_hash == hashlib.sha256(target.read_bytes()).hexdigest()


def test_eviction_picks_lowest_score_first_and_stubs_only_cold(
    config_dir: Path,
) -> None:
    cfg = _config(
        min_page_tokens=1,
        protect_recent_turns=1,
        context_budget=600,
        evict_target_ratio=0.8,
    )
    vm = VibeVM(session_id="evict-1", config_getter=lambda: cfg)
    messages = [
        _user("go"),
        _assistant_call("call-a", "web_fetch"),  # low importance (.35), oldest
        _tool_result("call-a", "web_fetch", "a" * 1200),
        _assistant_call("call-b", "read_file"),  # high importance (.7), newer
        _tool_result("call-b", "read_file", "b" * 1200),
        _user("next turn"),  # starts the protected round; nothing after it yet
    ]

    out = vm.apply(messages)

    by_tcid = {m.tool_call_id: m for m in out if m.role == Role.tool}
    assert by_tcid["call-a"].content.startswith(STUB_PREFIX)
    assert by_tcid["call-b"].content == "b" * 1200  # higher score: stays HOT

    stats = vm.snapshot().stats
    assert stats.evictions == 1


def test_protected_recent_turns_never_evicted(config_dir: Path) -> None:
    cfg = _config(min_page_tokens=1, protect_recent_turns=1, context_budget=1)
    vm = VibeVM(session_id="protect-1", config_getter=lambda: cfg)
    messages = [
        _user("first turn"),
        _assistant_call("call-old", "bash"),
        _tool_result("call-old", "bash", "o" * 800),
        _user("second turn"),  # starts the last (protected) round
        _assistant_call("call-new", "bash"),
        _tool_result("call-new", "bash", "n" * 800),
    ]

    out = vm.apply(messages)  # never raises despite an impossible budget

    by_tcid = {m.tool_call_id: m for m in out if m.role == Role.tool}
    assert by_tcid["call-old"].content.startswith(STUB_PREFIX)
    assert by_tcid["call-new"].content == "n" * 800  # protected: never evicted


def test_budget_respected_evicts_down_to_target(config_dir: Path) -> None:
    cfg = _config(
        min_page_tokens=1,
        protect_recent_turns=0,
        context_budget=1000,
        evict_target_ratio=0.8,
    )
    vm = VibeVM(session_id="budget-1", config_getter=lambda: cfg)
    contents = [f"{d}" * 2000 for d in ("1", "2", "3")]
    messages = [_user("go")]
    for i, content in enumerate(contents):
        messages.append(_assistant_call(f"call-{i}", "bash"))
        messages.append(_tool_result(f"call-{i}", "bash", content))

    out = vm.apply(messages)

    target = cfg.vibevm.context_budget * cfg.vibevm.evict_target_ratio
    assert _estimate_tokens(out) <= target
    surviving = [
        m.content for m in out if m.role == Role.tool and m.content in contents
    ]
    assert len(surviving) >= 1  # target was reachable without wiping everything


def test_apply_is_idempotent_with_zero_additional_writes(config_dir: Path) -> None:
    cfg = _config(
        min_page_tokens=1,
        protect_recent_turns=0,
        context_budget=200,
        evict_target_ratio=0.8,
    )
    vm = VibeVM(session_id="idem-1", config_getter=lambda: cfg)
    messages = [
        _user("go"),
        _assistant_call("call-1", "bash"),
        _tool_result("call-1", "bash", "1" * 2000),
        _assistant_call("call-2", "bash"),
        _tool_result("call-2", "bash", "2" * 2000),
    ]

    first = vm.apply(messages)

    store = vm._ensure_store()
    upsert = MagicMock(wraps=store.upsert_page)
    set_state = MagicMock(wraps=store.set_state)
    bump = MagicMock(wraps=store.bump_stat)
    set_stat = MagicMock(wraps=store.set_stat)
    store.upsert_page = upsert  # type: ignore[method-assign]
    store.set_state = set_state  # type: ignore[method-assign]
    store.bump_stat = bump  # type: ignore[method-assign]
    store.set_stat = set_stat  # type: ignore[method-assign]

    second = vm.apply(messages)

    upsert.assert_not_called()
    set_state.assert_not_called()
    bump.assert_not_called()
    set_stat.assert_not_called()
    assert [m.content for m in second] == [m.content for m in first]


def test_page_store_created_under_isolated_vibe_home(config_dir: Path) -> None:
    cfg = _config(min_page_tokens=1)
    vm = VibeVM(session_id="isolation-check", config_getter=lambda: cfg)
    messages = [
        _user("hi"),
        _assistant_call("call-1", "bash"),
        _tool_result("call-1", "bash", "x" * 400),
    ]

    vm.apply(messages)

    assert (config_dir / "vm" / "isolation-check.db").exists()


def test_recall_hit_bumps_stats_and_returns_content(config_dir: Path) -> None:
    cfg = _config(
        min_page_tokens=1,
        context_budget=1,
        evict_target_ratio=0.5,
        protect_recent_turns=0,
    )
    vm = VibeVM(session_id="recall-1", config_getter=lambda: cfg)
    messages = [
        _user("go"),
        _assistant_call("call-1", "bash"),
        _tool_result("call-1", "bash", "needle-in-haystack " * 50),
    ]
    vm.apply(messages)  # tiny budget: the only page is evicted to COLD

    outcome = vm.recall("needle")

    assert outcome.status == "ok"
    assert outcome.content is not None
    assert "needle-in-haystack" in outcome.content
    stats = vm.snapshot().stats
    assert stats.hits == 1
    assert stats.page_faults == 1


def test_recall_miss_bumps_misses_stat(config_dir: Path) -> None:
    cfg = _config(min_page_tokens=1)
    vm = VibeVM(session_id="recall-2", config_getter=lambda: cfg)
    vm.apply([_user("go")])  # nothing to register, but lazily opens the store

    outcome = vm.recall("nothing-registered-anywhere")

    assert outcome.status == "miss"
    assert vm.snapshot().stats.misses == 1


def test_freshly_created_pages_survive_even_over_hard_budget(config_dir: Path) -> None:
    # Only 2 tool calls total: the newest has 0 assistant messages after it,
    # the other has 1 -- both are below the age gate, and both are trivially
    # within the last-4-tool window too, so neither tier can touch them no
    # matter how far over budget the view is.
    cfg = _config(
        min_page_tokens=1,
        protect_recent_turns=1,
        context_budget=50,
        evict_target_ratio=0.5,
    )
    vm = VibeVM(session_id="age-1", config_getter=lambda: cfg)
    messages = [_user("go")]
    for i in range(2):
        messages.append(_assistant_call(f"call-{i}", "bash"))
        messages.append(_tool_result(f"call-{i}", "bash", f"{i}" * 2000))

    out = vm.apply(messages)  # never raises despite a budget it cannot meet

    by_tcid = {m.tool_call_id: m for m in out if m.role == Role.tool}
    for i in range(2):
        assert by_tcid[f"call-{i}"].content == f"{i}" * 2000
    assert vm.snapshot().stats.evictions == 0


def test_current_round_page_with_enough_age_evicts_over_hard_budget(
    config_dir: Path,
) -> None:
    # 6 tool calls in ONE round: tier 1 is always empty here (a single round
    # with protect_recent_turns>=1 protects everything by round boundary
    # alone). call-0 has 5 assistant messages after it and sits outside the
    # last-4 tool window, so tier 2's age+position rule allows it to be
    # evicted mid-turn -- the new capability this change adds.
    cfg = _config(
        min_page_tokens=1,
        protect_recent_turns=1,
        context_budget=200,
        evict_target_ratio=0.5,
    )
    vm = VibeVM(session_id="age-2", config_getter=lambda: cfg)
    messages = [_user("one big turn, many tool calls")]
    for i in range(6):
        messages.append(_assistant_call(f"call-{i}", "bash"))
        messages.append(_tool_result(f"call-{i}", "bash", f"{i}" * 1500))

    out = vm.apply(messages)

    by_tcid = {m.tool_call_id: m for m in out if m.role == Role.tool}
    assert by_tcid["call-0"].content.startswith(STUB_PREFIX)  # aged + out of window
    for i in range(2, 6):  # last 4 tool results: positionally exempt regardless of age
        assert by_tcid[f"call-{i}"].content == f"{i}" * 1500
    assert vm.snapshot().stats.evictions >= 1


def test_just_recalled_page_survives_the_thrash_regression(config_dir: Path) -> None:
    # Reproduces the original live-smoke bug: a page recalled moments ago must
    # not be immediately re-evicted just because it is technically outside a
    # round or position boundary -- it needs 2+ assistant turns to actually
    # age before tier 2 will consider it.
    cfg = _config(
        min_page_tokens=1,
        protect_recent_turns=1,
        context_budget=50,
        evict_target_ratio=0.5,
    )
    vm = VibeVM(session_id="age-3", config_getter=lambda: cfg)
    messages = [_user("investigate the bug")]
    for i in range(6):
        messages.append(_assistant_call(f"call-{i}", "bash"))
        messages.append(_tool_result(f"call-{i}", "bash", f"{i}" * 1500))
    messages.append(_assistant_call("call-recall", "recall_context"))
    messages.append(_tool_result("call-recall", "recall_context", "r" * 1500))

    out = vm.apply(messages)  # tiny budget: would love to evict everything

    by_tcid = {m.tool_call_id: m for m in out if m.role == Role.tool}
    assert by_tcid["call-recall"].content == "r" * 1500  # just recalled: survives


def test_rebind_same_session_id_is_a_no_op(config_dir: Path) -> None:
    cfg = _config(min_page_tokens=1)
    vm = VibeVM(session_id="same-1", config_getter=lambda: cfg)
    vm.apply([
        _user("go"),
        _assistant_call("call-1", "bash"),
        _tool_result("call-1", "bash", "x" * 400),
    ])
    store_before = vm._ensure_store()

    vm.rebind("same-1")

    assert vm._store is store_before  # same handle: no close/reopen
    assert vm.session_id == "same-1"


def test_rebind_different_session_id_closes_and_reopens_store(config_dir: Path) -> None:
    cfg = _config(min_page_tokens=1)
    vm = VibeVM(session_id="old-sess", config_getter=lambda: cfg)
    vm.apply([
        _user("go"),
        _assistant_call("call-1", "bash"),
        _tool_result("call-1", "bash", "x" * 400),
    ])

    vm.rebind("new-sess")

    assert vm.session_id == "new-sess"
    assert vm._store is None  # closed; will lazily reopen under the new id


def test_context_budget_recorded_once_and_shown_in_snapshot(config_dir: Path) -> None:
    cfg = _config(min_page_tokens=1, context_budget=12_345)
    vm = VibeVM(session_id="budget-stat-1", config_getter=lambda: cfg)
    messages = [
        _user("go"),
        _assistant_call("call-1", "bash"),
        _tool_result("call-1", "bash", "x" * 400),
    ]

    vm.apply(messages)
    assert vm.snapshot().stats.context_budget == 12_345

    store = vm._ensure_store()
    set_stat = MagicMock(wraps=store.set_stat)
    store.set_stat = set_stat  # type: ignore[method-assign]

    vm.apply(messages)  # unchanged config: must not write the stat again

    set_stat.assert_not_called()


def test_context_budget_rewritten_when_config_changes(config_dir: Path) -> None:
    cfg = _config(min_page_tokens=1, context_budget=100)
    vm = VibeVM(session_id="budget-stat-2", config_getter=lambda: cfg)
    vm.apply([_user("go")])
    assert vm.snapshot().stats.context_budget == 100

    assert cfg.vibevm is not None
    cfg.vibevm.context_budget = 200  # simulate a config change between calls
    vm.apply([_user("go")])

    assert vm.snapshot().stats.context_budget == 200


def test_recall_by_exact_page_id(config_dir: Path) -> None:
    cfg = _config(
        min_page_tokens=1,
        context_budget=1,
        evict_target_ratio=0.5,
        protect_recent_turns=0,
    )
    vm = VibeVM(session_id="recall-3", config_getter=lambda: cfg)
    messages = [
        _user("go"),
        _assistant_call("call-1", "bash"),
        _tool_result("call-1", "bash", "z" * 400),
    ]
    vm.apply(messages)
    page_id = vm.snapshot().pages[0].id

    outcome = vm.recall("", page_id=page_id)

    assert outcome.status == "ok"
    assert outcome.page_id == page_id


def test_recall_excerpt_contains_matched_line_verbatim_and_shrinks_page(
    config_dir: Path,
) -> None:
    distinctive = "ERROR 500: connection reset by peer at auth.py:88"
    content = _log_content(200, {100: distinctive})
    cfg = _config(
        min_page_tokens=1,
        context_budget=1,
        evict_target_ratio=0.5,
        protect_recent_turns=0,
    )
    vm = VibeVM(session_id="excerpt-1", config_getter=lambda: cfg)
    vm.apply([
        _user("go"),
        _assistant_call("call-1", "bash"),
        _tool_result("call-1", "bash", content),
    ])  # tiny budget: the page is evicted to COLD before recall

    outcome = vm.recall("connection reset")

    assert outcome.status == "ok"
    assert outcome.content is not None
    assert distinctive in outcome.content  # verbatim: never trimmed mid-line
    assert len(outcome.content) < len(content)
    assert outcome.note is not None
    assert "full=true" in outcome.note


def test_recall_full_true_bypasses_excerpting(config_dir: Path) -> None:
    distinctive = "ERROR 500: connection reset by peer at auth.py:88"
    content = _log_content(200, {100: distinctive})
    cfg = _config(
        min_page_tokens=1,
        context_budget=1,
        evict_target_ratio=0.5,
        protect_recent_turns=0,
    )
    vm = VibeVM(session_id="excerpt-2", config_getter=lambda: cfg)
    vm.apply([
        _user("go"),
        _assistant_call("call-1", "bash"),
        _tool_result("call-1", "bash", content),
    ])  # tiny budget: the only page is evicted to COLD

    outcome = vm.recall("connection reset", full=True)

    assert outcome.status == "ok"  # not already_hot: under the full=True cap
    assert outcome.content == content
    assert outcome.note is None


def test_recall_small_page_returns_whole_content_no_excerpt_note(
    config_dir: Path,
) -> None:
    cfg = _config(
        min_page_tokens=1,
        context_budget=1,
        evict_target_ratio=0.5,
        protect_recent_turns=0,
    )
    vm = VibeVM(session_id="excerpt-3", config_getter=lambda: cfg)
    content = "short tool output\n" * 5
    vm.apply([
        _user("go"),
        _assistant_call("call-1", "bash"),
        _tool_result("call-1", "bash", content),
    ])  # tiny budget: the only page is evicted to COLD

    outcome = vm.recall("tool output")

    assert outcome.status == "ok"  # not already_hot: excerpting is size-based here
    assert outcome.content == content
    assert outcome.note is None


def test_recall_excerpt_merges_overlapping_windows(config_dir: Path) -> None:
    first = "ERROR: first distinctive failure line"
    second = "ERROR: second distinctive failure line"
    content = _log_content(200, {100: first, 103: second})  # within +-3: overlaps
    cfg = _config(min_page_tokens=1)
    vm = VibeVM(session_id="excerpt-4", config_getter=lambda: cfg)
    vm.apply([
        _user("go"),
        _assistant_call("call-1", "bash"),
        _tool_result("call-1", "bash", content),
    ])

    outcome = vm.recall("distinctive failure")

    assert outcome.content is not None
    assert first in outcome.content
    assert second in outcome.content
    assert "lines skipped" not in outcome.content  # merged: no gap between them


def test_recall_excerpt_falls_back_to_middle_truncation_without_query_terms(
    config_dir: Path,
) -> None:
    # page_id bypasses search, so this isolates the excerpter's own "no terms"
    # fallback from store.search's separate "empty query" miss behavior.
    content = _log_content(200, {})
    cfg = _config(
        min_page_tokens=1,
        context_budget=1,
        evict_target_ratio=0.5,
        protect_recent_turns=0,
    )
    vm = VibeVM(session_id="excerpt-5", config_getter=lambda: cfg)
    vm.apply([
        _user("go"),
        _assistant_call("call-1", "bash"),
        _tool_result("call-1", "bash", content),
    ])  # tiny budget: the only page is evicted to COLD
    page_id = vm.snapshot().pages[0].id

    outcome = vm.recall("", page_id=page_id)  # no query terms at all

    assert outcome.status == "ok"  # not already_hot: keeps the pass-full=true note
    assert outcome.content is not None
    assert len(outcome.content) < len(content)
    assert outcome.note is not None
    assert "full=true" in outcome.note


def test_recall_full_true_over_cap_truncates_middle(config_dir: Path) -> None:
    content = "z" * 30000  # 7500 tokens: over the 6000 full=True cap
    cfg = _config(
        min_page_tokens=1,
        context_budget=1,
        evict_target_ratio=0.5,
        protect_recent_turns=0,
    )
    vm = VibeVM(session_id="full-cap-1", config_getter=lambda: cfg)
    vm.apply([
        _user("go"),
        _assistant_call("call-1", "bash"),
        _tool_result("call-1", "bash", content),
    ])  # tiny budget: the only page is evicted to COLD
    page_id = vm.snapshot().pages[0].id

    outcome = vm.recall("", page_id=page_id, full=True)

    assert outcome.status == "ok"
    assert outcome.content is not None
    assert len(outcome.content) < len(content)
    assert outcome.content.startswith("z" * 100)  # head preserved
    assert outcome.content.endswith("z" * 100)  # tail preserved
    assert outcome.note == (
        "Showing ~6k of ~7.5k tokens (middle omitted); "
        "use a specific query to excerpt the exact region."
    )


def test_recall_full_true_under_cap_returns_complete_content(config_dir: Path) -> None:
    content = "z" * 24000  # exactly 6000 tokens: at the full=True cap
    cfg = _config(
        min_page_tokens=1,
        context_budget=1,
        evict_target_ratio=0.5,
        protect_recent_turns=0,
    )
    vm = VibeVM(session_id="full-cap-2", config_getter=lambda: cfg)
    vm.apply([
        _user("go"),
        _assistant_call("call-1", "bash"),
        _tool_result("call-1", "bash", content),
    ])  # tiny budget: the only page is evicted to COLD
    page_id = vm.snapshot().pages[0].id

    outcome = vm.recall("", page_id=page_id, full=True)

    assert outcome.status == "ok"
    assert outcome.content == content
    assert outcome.note is None


def test_recall_already_hot_returns_excerpt_even_with_full_true(
    config_dir: Path,
) -> None:
    distinctive = "ERROR 500: connection reset by peer at auth.py:88"
    content = _log_content(200, {100: distinctive})
    cfg = _config(min_page_tokens=1)  # huge default budget: page never evicted
    vm = VibeVM(session_id="hot-1", config_getter=lambda: cfg)
    vm.apply([
        _user("go"),
        _assistant_call("call-1", "bash"),
        _tool_result("call-1", "bash", content),
    ])

    outcome = vm.recall("connection reset", full=True)

    assert outcome.status == "already_hot"
    assert outcome.content is not None
    assert outcome.content != content  # excerpt, not the full text
    assert len(outcome.content) < len(content)
    assert distinctive in outcome.content  # excerpt still finds the query match
    assert outcome.note == (
        "This page is already present in your context in full; excerpt shown."
    )

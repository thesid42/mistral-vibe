from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock

from pydantic import BaseModel

from vibe.core.types import FunctionCall, LLMMessage, Role, ToolCall
from vibe.core.vibevm.pager import STUB_PREFIX, VibeVM, _estimate_tokens


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
    store.upsert_page = upsert  # type: ignore[method-assign]
    store.set_state = set_state  # type: ignore[method-assign]
    store.bump_stat = bump  # type: ignore[method-assign]

    second = vm.apply(messages)

    upsert.assert_not_called()
    set_state.assert_not_called()
    bump.assert_not_called()
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

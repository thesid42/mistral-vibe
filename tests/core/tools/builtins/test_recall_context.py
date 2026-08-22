from __future__ import annotations

from collections.abc import Iterator
import json
from pathlib import Path
import uuid

from pydantic import BaseModel
import pytest

from tests.mock.utils import collect_result
from vibe.core.tools.base import BaseToolState, InvokeContext
from vibe.core.tools.builtins.recall_context import (
    RecallContext,
    RecallContextArgs,
    RecallContextConfig,
)
from vibe.core.types import FunctionCall, LLMMessage, Role, ToolCall
from vibe.core.vibevm import registry as vibevm_registry
from vibe.core.vibevm.pager import VibeVM


class _FakeVibeVMConfig(BaseModel):
    enabled: bool = True
    context_budget: int = 100_000
    evict_target_ratio: float = 0.8
    min_page_tokens: int = 1
    protect_recent_turns: int = 0


class _FakeConfig(BaseModel):
    vibevm: _FakeVibeVMConfig | None = None


def _config(**overrides: object) -> _FakeConfig:
    return _FakeConfig(vibevm=_FakeVibeVMConfig(**overrides))


def _user(content: str) -> LLMMessage:
    return LLMMessage(role=Role.user, content=content)


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


def _make_tool() -> RecallContext:
    return RecallContext(
        config_getter=lambda: RecallContextConfig(), state=BaseToolState()
    )


@pytest.fixture
def session_id() -> Iterator[str]:
    sid = f"recall-ctx-test-{uuid.uuid4().hex}"
    yield sid
    vibevm_registry.unregister(sid)


@pytest.mark.asyncio
async def test_unavailable_when_no_vm_registered(session_id: str) -> None:
    tool = _make_tool()
    ctx = InvokeContext(tool_call_id="t1", session_id=session_id)

    result = await collect_result(tool.run(RecallContextArgs(query="anything"), ctx))

    assert result.status == "unavailable"
    assert result.note is not None
    assert result.content is None


@pytest.mark.asyncio
async def test_recall_hit_returns_content_and_counts_stats(
    config_dir: Path, session_id: str
) -> None:
    cfg = _config(context_budget=1, evict_target_ratio=0.5)
    vm = VibeVM(session_id=session_id, config_getter=lambda: cfg)
    messages = [
        _user("go"),
        _assistant_call("call-1", "bash"),
        _tool_result("call-1", "bash", "needle-in-haystack " * 50),
    ]
    vm.apply(messages)  # tiny budget: the only page is evicted to COLD
    vibevm_registry.register(session_id, vm)

    tool = _make_tool()
    ctx = InvokeContext(tool_call_id="t1", session_id=session_id)
    result = await collect_result(tool.run(RecallContextArgs(query="needle"), ctx))

    assert result.status == "ok"
    assert result.content is not None
    assert "needle-in-haystack" in result.content

    stats = vm.snapshot().stats
    assert stats.hits == 1
    assert stats.page_faults == 1


@pytest.mark.asyncio
async def test_recall_miss_returns_miss_status(
    config_dir: Path, session_id: str
) -> None:
    cfg = _config()
    vm = VibeVM(session_id=session_id, config_getter=lambda: cfg)
    vm.apply([_user("go")])  # nothing to register, but lazily opens the store
    vibevm_registry.register(session_id, vm)

    tool = _make_tool()
    ctx = InvokeContext(tool_call_id="t1", session_id=session_id)
    result = await collect_result(
        tool.run(RecallContextArgs(query="nothing-registered-anywhere"), ctx)
    )

    assert result.status == "miss"
    assert result.content is None

    assert vm.snapshot().stats.misses == 1


@pytest.mark.asyncio
async def test_recall_stale_refreshed_returns_current_file_content(
    config_dir: Path, tmp_path: Path, session_id: str
) -> None:
    target = tmp_path / "example.py"
    original_text = "print('original')\n" * 50
    target.write_text(original_text, encoding="utf-8")

    cfg = _config()
    vm = VibeVM(session_id=session_id, config_getter=lambda: cfg)
    args = json.dumps({"file_path": str(target)})
    messages = [
        _user("go"),
        _assistant_call("call-1", "read_file", args),
        _tool_result("call-1", "read_file", original_text),
    ]
    vm.apply(messages)
    vibevm_registry.register(session_id, vm)

    updated_text = "print('updated')\n" * 50
    target.write_text(updated_text, encoding="utf-8")

    tool = _make_tool()
    ctx = InvokeContext(tool_call_id="t1", session_id=session_id)
    result = await collect_result(tool.run(RecallContextArgs(query="original"), ctx))

    assert result.status == "stale_refreshed"
    assert result.content is not None
    assert "updated" in result.content
    assert "original" not in result.content
    assert result.note is not None
    assert result.source == str(target)


@pytest.mark.asyncio
async def test_recall_returns_windowed_excerpt_for_large_page(
    config_dir: Path, session_id: str
) -> None:
    distinctive = "ERROR 500: connection reset by peer at auth.py:88"
    content = _log_content(200, {100: distinctive})
    cfg = _config(context_budget=1, evict_target_ratio=0.5)
    vm = VibeVM(session_id=session_id, config_getter=lambda: cfg)
    vm.apply([
        _user("go"),
        _assistant_call("call-1", "bash"),
        _tool_result("call-1", "bash", content),
    ])  # tiny budget: the only page is evicted to COLD
    vibevm_registry.register(session_id, vm)

    tool = _make_tool()
    ctx = InvokeContext(tool_call_id="t1", session_id=session_id)
    result = await collect_result(
        tool.run(RecallContextArgs(query="connection reset"), ctx)
    )

    assert result.status == "ok"
    assert result.content is not None
    assert distinctive in result.content  # verbatim: never trimmed mid-line
    assert len(result.content) < len(content)
    assert result.note is not None
    assert "full=true" in result.note


@pytest.mark.asyncio
async def test_recall_full_true_returns_complete_content(
    config_dir: Path, session_id: str
) -> None:
    distinctive = "ERROR 500: connection reset by peer at auth.py:88"
    content = _log_content(200, {100: distinctive})
    cfg = _config()
    vm = VibeVM(session_id=session_id, config_getter=lambda: cfg)
    vm.apply([
        _user("go"),
        _assistant_call("call-1", "bash"),
        _tool_result("call-1", "bash", content),
    ])
    vibevm_registry.register(session_id, vm)

    tool = _make_tool()
    ctx = InvokeContext(tool_call_id="t1", session_id=session_id)
    result = await collect_result(
        tool.run(RecallContextArgs(query="connection reset", full=True), ctx)
    )

    assert result.content == content


@pytest.mark.asyncio
async def test_recall_small_page_returns_whole_content(
    config_dir: Path, session_id: str
) -> None:
    cfg = _config()
    vm = VibeVM(session_id=session_id, config_getter=lambda: cfg)
    content = "short tool output\n" * 5
    vm.apply([
        _user("go"),
        _assistant_call("call-1", "bash"),
        _tool_result("call-1", "bash", content),
    ])
    vibevm_registry.register(session_id, vm)

    tool = _make_tool()
    ctx = InvokeContext(tool_call_id="t1", session_id=session_id)
    result = await collect_result(tool.run(RecallContextArgs(query="tool output"), ctx))

    assert result.content == content


@pytest.mark.asyncio
async def test_recall_stale_refreshed_combines_with_excerpt(
    config_dir: Path, tmp_path: Path, session_id: str
) -> None:
    target = tmp_path / "example.py"
    original_text = "print('original')\n" * 5
    target.write_text(original_text, encoding="utf-8")

    cfg = _config()
    vm = VibeVM(session_id=session_id, config_getter=lambda: cfg)
    args = json.dumps({"file_path": str(target)})
    vm.apply([
        _user("go"),
        _assistant_call("call-1", "read_file", args),
        _tool_result("call-1", "read_file", original_text),
    ])
    vibevm_registry.register(session_id, vm)
    page_id = vm.snapshot().pages[0].id

    distinctive = "def handle_request(): raise ConnectionResetError()"
    updated_text = _log_content(200, {100: distinctive})
    target.write_text(updated_text, encoding="utf-8")

    tool = _make_tool()
    ctx = InvokeContext(tool_call_id="t1", session_id=session_id)
    result = await collect_result(
        tool.run(RecallContextArgs(query="ConnectionResetError", page_id=page_id), ctx)
    )

    assert result.status == "stale_refreshed"
    assert result.content is not None
    assert distinctive in result.content
    assert len(result.content) < len(updated_text)
    assert result.note is not None
    assert "showing current content" in result.note  # stale note preserved
    assert "full=true" in result.note  # excerpt note appended

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

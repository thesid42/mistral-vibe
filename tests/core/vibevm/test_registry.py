from __future__ import annotations

from vibe.core.vibevm import registry
from vibe.core.vibevm.pager import VibeVM


def _vm(session_id: str) -> VibeVM:
    return VibeVM(session_id=session_id, config_getter=lambda: object())


def test_register_and_get_round_trip() -> None:
    vm = _vm("sess-a")
    registry.register("sess-a", vm)
    try:
        assert registry.get("sess-a") is vm
    finally:
        registry.unregister("sess-a")


def test_get_with_none_and_single_entry_returns_it() -> None:
    vm = _vm("sess-b")
    registry.register("sess-b", vm)
    try:
        assert registry.get(None) is vm
    finally:
        registry.unregister("sess-b")


def test_get_with_none_and_multiple_entries_returns_none() -> None:
    vm1 = _vm("sess-c1")
    vm2 = _vm("sess-c2")
    registry.register("sess-c1", vm1)
    registry.register("sess-c2", vm2)
    try:
        assert registry.get(None) is None
    finally:
        registry.unregister("sess-c1")
        registry.unregister("sess-c2")


def test_get_with_none_and_no_entries_returns_none() -> None:
    assert registry.get(None) is None


def test_unregister_missing_key_is_a_no_op() -> None:
    registry.unregister("never-registered-xyz")  # must not raise


def test_register_drops_stale_keys_pointing_to_the_same_vm() -> None:
    vm = _vm("old-id")
    registry.register("old-id", vm)
    try:
        registry.register("new-id", vm)  # simulates VibeVM.rebind + re-register

        assert registry.get("new-id") is vm
        assert registry.get("old-id") is None  # stale key was dropped
        assert registry.get(None) is vm  # single-entry fallback still works
    finally:
        registry.unregister("new-id")
        registry.unregister("old-id")


def test_register_does_not_disturb_other_sessions_vm() -> None:
    other = _vm("other-session")
    mine = _vm("old-id-2")
    registry.register("other-session", other)
    registry.register("old-id-2", mine)
    try:
        registry.register("new-id-2", mine)

        assert registry.get("other-session") is other  # untouched
        assert registry.get("new-id-2") is mine
        assert registry.get("old-id-2") is None
    finally:
        registry.unregister("other-session")
        registry.unregister("new-id-2")
        registry.unregister("old-id-2")

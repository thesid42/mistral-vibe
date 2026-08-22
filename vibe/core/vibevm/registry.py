from __future__ import annotations

from vibe.core.vibevm.pager import VibeVM

_REGISTRY: dict[str, VibeVM] = {}


def register(session_id: str, vm: VibeVM) -> None:
    stale = [
        key for key, value in _REGISTRY.items() if value is vm and key != session_id
    ]
    for key in stale:
        del _REGISTRY[key]
    _REGISTRY[session_id] = vm


def get(session_id: str | None) -> VibeVM | None:
    if session_id is not None:
        return _REGISTRY.get(session_id)
    if len(_REGISTRY) == 1:
        return next(iter(_REGISTRY.values()))
    return None


def unregister(session_id: str) -> None:
    _REGISTRY.pop(session_id, None)

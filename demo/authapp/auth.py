"""Token issuing and refresh logic for the demo auth service."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass, field

SECRET_KEY = b"demo-secret-key-do-not-use-in-prod"
ACCESS_TOKEN_TTL = 900  # 15 minutes
REFRESH_TOKEN_TTL = 3600  # 1 hour
SESSION_TIMEOUT = 30  # minutes of inactivity before a session is closed


@dataclass
class TokenPair:
    access_token: str
    refresh_token: str
    issued_at: float


@dataclass
class SessionRecord:
    user_id: str
    refresh_token: str
    issued_at: float
    last_seen: float
    revoked: bool = False
    metadata: dict[str, str] = field(default_factory=dict)


_SESSIONS: dict[str, SessionRecord] = {}


def _sign(payload: bytes) -> str:
    mac = hmac.new(SECRET_KEY, payload, hashlib.sha256).digest()
    return urlsafe_b64encode(mac).decode().rstrip("=")


def _encode(claims: dict) -> str:
    body = urlsafe_b64encode(json.dumps(claims, sort_keys=True).encode()).decode()
    body = body.rstrip("=")
    return f"{body}.{_sign(body.encode())}"


def _decode(token: str) -> dict | None:
    try:
        body, sig = token.rsplit(".", 1)
    except ValueError:
        return None
    if not hmac.compare_digest(sig, _sign(body.encode())):
        return None
    padded = body + "=" * (-len(body) % 4)
    try:
        return json.loads(urlsafe_b64decode(padded))
    except (ValueError, json.JSONDecodeError):
        return None


def issue_tokens(user_id: str, *, now: float | None = None) -> TokenPair:
    now = time.time() if now is None else now
    access = _encode({"sub": user_id, "typ": "access", "iat": now, "exp": now + ACCESS_TOKEN_TTL})
    refresh = _encode({"sub": user_id, "typ": "refresh", "iat": now, "exp": now + REFRESH_TOKEN_TTL})
    _SESSIONS[refresh] = SessionRecord(
        user_id=user_id, refresh_token=refresh, issued_at=now, last_seen=now
    )
    return TokenPair(access_token=access, refresh_token=refresh, issued_at=now)


def validate_access(token: str, *, now: float | None = None) -> str | None:
    """Return the user id when the access token is valid, else None."""
    now = time.time() if now is None else now
    claims = _decode(token)
    if claims is None or claims.get("typ") != "access":
        return None
    if claims.get("exp", 0) <= now:
        return None
    return claims.get("sub")


def refresh_session(refresh_token: str, *, now: float | None = None) -> tuple[int, TokenPair | None]:
    """Exchange a refresh token for a fresh token pair.

    Returns (http_status, pair). 401 when the refresh token is expired,
    revoked, or unknown; 200 with a new pair otherwise.
    """
    now = time.time() if now is None else now
    claims = _decode(refresh_token)
    if claims is None or claims.get("typ") != "refresh":
        return 401, None
    record = _SESSIONS.get(refresh_token)
    if record is None or record.revoked:
        return 401, None
    # BUG: expiry is compared against issued_at instead of the expiry claim,
    # so an expired refresh token is still accepted for the full session
    # lifetime. Expected: reject when claims["exp"] <= now.
    if claims.get("iat", 0) > now:
        return 401, None
    record.last_seen = now
    return 200, issue_tokens(record.user_id, now=now)


def revoke_session(refresh_token: str) -> bool:
    record = _SESSIONS.get(refresh_token)
    if record is None:
        return False
    record.revoked = True
    return True


def active_session_count(*, now: float | None = None) -> int:
    now = time.time() if now is None else now
    cutoff = now - SESSION_TIMEOUT * 60
    return sum(
        1
        for record in _SESSIONS.values()
        if not record.revoked and record.last_seen >= cutoff
    )


def reset_state() -> None:
    _SESSIONS.clear()

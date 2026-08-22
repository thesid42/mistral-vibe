"""Tests for the demo auth service. One test fails: the refresh-token expiry bug."""

import time

import pytest

import auth


@pytest.fixture(autouse=True)
def clean_state():
    auth.reset_state()
    yield
    auth.reset_state()


NOW = 1_755_000_000.0


def test_issue_returns_distinct_tokens():
    pair = auth.issue_tokens("user-1", now=NOW)
    assert pair.access_token != pair.refresh_token
    assert pair.issued_at == NOW


def test_access_token_valid_within_ttl():
    pair = auth.issue_tokens("user-1", now=NOW)
    assert auth.validate_access(pair.access_token, now=NOW + 10) == "user-1"


def test_access_token_expires():
    pair = auth.issue_tokens("user-1", now=NOW)
    assert auth.validate_access(pair.access_token, now=NOW + auth.ACCESS_TOKEN_TTL + 1) is None


def test_access_token_rejects_tampering():
    pair = auth.issue_tokens("user-1", now=NOW)
    tampered = pair.access_token[:-2] + "xx"
    assert auth.validate_access(tampered, now=NOW + 10) is None


def test_refresh_rotates_tokens():
    pair = auth.issue_tokens("user-1", now=NOW)
    status, new_pair = auth.refresh_session(pair.refresh_token, now=NOW + 60)
    assert status == 200
    assert new_pair is not None
    assert new_pair.refresh_token != pair.refresh_token


def test_refresh_rejects_unknown_token():
    status, new_pair = auth.refresh_session("not-a-token", now=NOW)
    assert status == 401
    assert new_pair is None


def test_refresh_rejects_revoked_session():
    pair = auth.issue_tokens("user-1", now=NOW)
    assert auth.revoke_session(pair.refresh_token)
    status, new_pair = auth.refresh_session(pair.refresh_token, now=NOW + 60)
    assert status == 401
    assert new_pair is None


def test_refresh_token_expires():
    """An expired refresh token must be rejected with 401."""
    pair = auth.issue_tokens("user-1", now=NOW)
    expired_at = NOW + auth.REFRESH_TOKEN_TTL + 5
    status, new_pair = auth.refresh_session(pair.refresh_token, now=expired_at)
    assert status == 401, (
        f"expired refresh token was accepted: got HTTP {status}, expected 401 "
        f"(token issued at {NOW}, refreshed at {expired_at}, "
        f"ttl {auth.REFRESH_TOKEN_TTL}s)"
    )
    assert new_pair is None


def test_active_session_count_ignores_idle_sessions():
    auth.issue_tokens("user-1", now=NOW)
    auth.issue_tokens("user-2", now=NOW)
    recent = NOW + auth.SESSION_TIMEOUT * 60 - 1
    assert auth.active_session_count(now=recent) == 2
    idle = NOW + auth.SESSION_TIMEOUT * 60 + 1
    assert auth.active_session_count(now=idle) == 0


def test_revoke_unknown_token_returns_false():
    assert auth.revoke_session("missing") is False


def test_session_count_after_revocation():
    pair = auth.issue_tokens("user-1", now=NOW)
    auth.issue_tokens("user-2", now=NOW)
    auth.revoke_session(pair.refresh_token)
    assert auth.active_session_count(now=NOW + 10) == 1

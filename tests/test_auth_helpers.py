"""Unit tests for auth helper functions (env, cookies, bearer tokens)."""

from __future__ import annotations

import pytest
from starlette.requests import Request

from api.auth import (
    auth_enabled,
    bearer_token_from_request,
    cookie_secure,
    hash_token,
    normalize_email,
    session_token_from_request,
)


def _request(headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": headers or [],
        }
    )


class TestAuthEnvHelpers:
    def test_auth_enabled_explicit_on(self, monkeypatch):
        monkeypatch.setenv("AUTH_ENABLED", "1")
        monkeypatch.setenv("DEV", "1")
        assert auth_enabled() is True

    def test_auth_enabled_explicit_off(self, monkeypatch):
        monkeypatch.setenv("AUTH_ENABLED", "0")
        monkeypatch.setenv("DEV", "0")
        assert auth_enabled() is False

    def test_auth_enabled_default_on_when_dev_zero(self, monkeypatch):
        monkeypatch.delenv("AUTH_ENABLED", raising=False)
        monkeypatch.setenv("DEV", "0")
        assert auth_enabled() is True

    def test_auth_enabled_default_off_when_dev_one(self, monkeypatch):
        monkeypatch.delenv("AUTH_ENABLED", raising=False)
        monkeypatch.setenv("DEV", "1")
        assert auth_enabled() is False

    def test_normalize_email_lowercases_and_trims(self):
        assert normalize_email("  Alice@Example.COM ") == "alice@example.com"


class TestCookieSecure:
    def test_cookie_secure_from_env(self, monkeypatch):
        monkeypatch.setenv("AUTH_COOKIE_SECURE", "1")
        assert cookie_secure() is True
        assert cookie_secure(_request()) is True

    def test_cookie_secure_from_x_forwarded_proto(self, monkeypatch):
        monkeypatch.delenv("AUTH_COOKIE_SECURE", raising=False)
        req = _request([(b"x-forwarded-proto", b"https")])
        assert cookie_secure(req) is True

    def test_cookie_secure_false_without_https(self, monkeypatch):
        monkeypatch.delenv("AUTH_COOKIE_SECURE", raising=False)
        assert cookie_secure(_request()) is False


class TestBearerTokenParsing:
    def test_bearer_token_from_authorization_header(self):
        req = _request([(b"authorization", b"Bearer abc.def.ghi")])
        assert bearer_token_from_request(req) == "abc.def.ghi"

    def test_bearer_token_ignored_when_malformed(self):
        req = _request([(b"authorization", b"Basic dXNlcjpwYXNz")])
        assert bearer_token_from_request(req) is None

    def test_session_token_prefers_cookie_over_bearer(self):
        req = _request(
            [
                (b"cookie", b"session_token=cookie-tok"),
                (b"authorization", b"Bearer bearer-tok"),
            ]
        )
        assert session_token_from_request(req) == "cookie-tok"

    def test_hash_token_is_sha256_hex(self):
        import hashlib

        raw = "my-secret-token"
        assert hash_token(raw) == hashlib.sha256(raw.encode()).hexdigest()

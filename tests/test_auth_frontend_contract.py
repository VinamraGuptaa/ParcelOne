"""Contract tests: frontend auth session handling matches backend expectations."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API_CLIENT = ROOT / "frontend" / "src" / "api" / "client.ts"
AUTH_CONTEXT = ROOT / "frontend" / "src" / "context" / "AuthContext.tsx"
REQUIRE_AUTH = ROOT / "frontend" / "src" / "components" / "auth" / "RequireAuth.tsx"
APP_TSX = ROOT / "frontend" / "src" / "App.tsx"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestAuthApiClientContract:
    def test_api_client_uses_local_storage_for_session_token(self):
        src = _read(API_CLIENT)
        assert "localStorage" in src
        assert "plotwise_session_token" in src
        assert "sessionStorage" in src

    def test_api_client_sends_credentials_and_bearer(self):
        src = _read(API_CLIENT)
        assert "credentials: 'include'" in src
        assert "Authorization" in src
        assert "Bearer" in src

    def test_api_get_throws_api_error_with_status(self):
        src = _read(API_CLIENT)
        assert "class ApiError" in src
        assert "throw new ApiError" in src
        assert "apiGet" in src

    def test_api_post_throws_api_error_with_status(self):
        src = _read(API_CLIENT)
        assert "throw new ApiError(message, res.status)" in src


class TestAuthContextContract:
    def test_auth_context_caches_user_for_refresh(self):
        src = _read(AUTH_CONTEXT)
        assert "plotwise_user_cache" in src
        assert "loadCachedUser" in src
        assert "cacheUser" in src

    def test_auth_context_restores_user_when_token_present_on_mount(self):
        src = _read(AUTH_CONTEXT)
        assert "getSessionToken() ? loadCachedUser()" in src

    def test_auth_context_only_clears_token_on_401(self):
        src = _read(AUTH_CONTEXT)
        assert "err instanceof ApiError && err.status === 401" in src
        assert "setSessionToken(null)" in src

    def test_auth_context_stores_token_from_login_and_register(self):
        src = _read(AUTH_CONTEXT)
        assert "applySessionFromAuthResponse" in src
        assert "me.session_token" in src

    def test_auth_context_logout_clears_storage(self):
        src = _read(AUTH_CONTEXT)
        assert "setSessionToken(null)" in src
        assert "cacheUser(null)" in src


class TestAuthRoutingContract:
    def test_app_has_login_signup_and_protected_routes(self):
        src = _read(APP_TSX)
        assert 'path="/login"' in src
        assert 'path="/signup"' in src
        assert "<RequireAuth />" in src or "RequireAuth" in src

    def test_require_auth_waits_for_loading_before_redirect(self):
        src = _read(REQUIRE_AUTH)
        assert "loading" in src
        assert 'Navigate to="/login"' in src

"""Authentication: registration, login, server-side cryptographic sessions."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.models import AuthSession, User
from api.schemas import (
    AuthConfigResponse,
    AuthCredentialsRequest,
    AuthRegisterRequest,
    AuthUserResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

SESSION_COOKIE = "session_token"
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def auth_enabled() -> bool:
    """Auth on when AUTH_ENABLED=1, or by default when DEV=0 (Docker/AWS production)."""
    raw = os.getenv("AUTH_ENABLED")
    if raw is not None and raw.strip() != "":
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return os.getenv("DEV", "0").strip().lower() not in {"1", "true", "yes", "on"}


def allow_register() -> bool:
    return os.getenv("AUTH_ALLOW_REGISTER", "1").strip().lower() in {"1", "true", "yes", "on"}


def session_max_age_seconds() -> int:
    try:
        return max(60, int(os.getenv("AUTH_SESSION_MAX_AGE", "604800")))
    except ValueError:
        return 604800


def cookie_secure(request: Request | None = None) -> bool:
    """Secure cookies when AUTH_COOKIE_SECURE=1 or ALB terminates HTTPS (X-Forwarded-Proto)."""
    raw = os.getenv("AUTH_COOKIE_SECURE")
    if raw is not None and raw.strip() != "":
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if request is not None:
        forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
        if forwarded == "https":
            return True
    return False


def bearer_token_from_request(request: Request) -> str | None:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        return token or None
    return None


def session_token_from_request(request: Request) -> str | None:
    return request.cookies.get(SESSION_COOKIE) or bearer_token_from_request(request)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())


async def purge_expired_sessions(db: AsyncSession) -> int:
    now = _utc_now()
    result = await db.execute(delete(AuthSession).where(AuthSession.expires_at <= now))
    await db.commit()
    return result.rowcount or 0


async def create_session(db: AsyncSession, user_id: str) -> str:
    raw_token = secrets.token_urlsafe(32)
    expires_at = _utc_now() + timedelta(seconds=session_max_age_seconds())
    db.add(
        AuthSession(
            user_id=user_id,
            token_hash=hash_token(raw_token),
            expires_at=expires_at,
        )
    )
    await db.commit()
    return raw_token


async def revoke_session(db: AsyncSession, raw_token: str | None) -> None:
    if not raw_token:
        return
    await db.execute(delete(AuthSession).where(AuthSession.token_hash == hash_token(raw_token)))
    await db.commit()


def set_session_cookie(response: Response, raw_token: str, request: Request | None = None) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=raw_token,
        httponly=True,
        secure=cookie_secure(request),
        samesite="lax",
        max_age=session_max_age_seconds(),
        path="/",
    )


def clear_session_cookie(response: Response, request: Request | None = None) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE,
        path="/",
        secure=cookie_secure(request),
        samesite="lax",
    )


async def resolve_user_from_request(request: Request, db: AsyncSession) -> User | None:
    raw_token = session_token_from_request(request)
    if not raw_token:
        return None

    now = _utc_now()
    result = await db.execute(
        select(AuthSession, User)
        .join(User, User.id == AuthSession.user_id)
        .where(AuthSession.token_hash == hash_token(raw_token))
    )
    row = result.first()
    if row is None:
        return None

    session_row, user = row
    if _as_utc(session_row.expires_at) <= now:
        await db.execute(delete(AuthSession).where(AuthSession.id == session_row.id))
        await db.commit()
        return None

    # Sliding expiry — keep active sessions alive across page refreshes.
    session_row.expires_at = now + timedelta(seconds=session_max_age_seconds())
    await db.commit()
    return user


async def get_current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User | None:
    if not auth_enabled():
        return None
    user = await resolve_user_from_request(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return user


async def get_optional_current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User | None:
    if not auth_enabled():
        return None
    return await resolve_user_from_request(request, db)


async def get_current_admin(
    current_user: User | None = Depends(get_current_user),
) -> User:
    if current_user is None or not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    return current_user


def user_response(
    user: User,
    *,
    auth_enabled_flag: bool = True,
    session_token: str | None = None,
) -> AuthUserResponse:
    return AuthUserResponse(
        user_id=user.id,
        email=user.email,
        auth_enabled=auth_enabled_flag,
        is_admin=bool(user.is_admin),
        session_token=session_token,
    )


async def ensure_admin_account(db: AsyncSession) -> None:
    """Create or promote the configured admin user from env on startup."""
    email_raw = os.getenv("AUTH_ADMIN_EMAIL", "").strip()
    password = os.getenv("AUTH_ADMIN_PASSWORD", "")
    if not email_raw or not password:
        return
    if len(password) < 8:
        logger.warning("AUTH_ADMIN_PASSWORD must be at least 8 characters — admin bootstrap skipped.")
        return

    email = normalize_email(email_raw)
    if not _EMAIL_RE.match(email):
        logger.warning("AUTH_ADMIN_EMAIL is invalid — admin bootstrap skipped.")
        return

    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(email=email, password_hash=hash_password(password), is_admin=True)
        db.add(user)
        await db.commit()
        logger.info("Created admin account: email=%s", email)
        return

    changed = False
    if not user.is_admin:
        user.is_admin = True
        changed = True
    if not verify_password(password, user.password_hash):
        user.password_hash = hash_password(password)
        changed = True
    if changed:
        await db.commit()
        logger.info("Updated admin account: email=%s", email)


@router.get("/config", response_model=AuthConfigResponse)
async def auth_config():
    return AuthConfigResponse(auth_enabled=auth_enabled(), allow_register=allow_register())


@router.post("/register", response_model=AuthUserResponse)
async def register(
    body: AuthRegisterRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    if not auth_enabled():
        raise HTTPException(status_code=404, detail="Authentication is disabled.")
    if not allow_register():
        raise HTTPException(status_code=403, detail="Registration is closed.")

    email = normalize_email(body.email)
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=422, detail="Invalid email address.")

    existing = await db.execute(select(User).where(User.email == email))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="An account with this email already exists.")

    user = User(email=email, password_hash=hash_password(body.password), is_admin=False)
    db.add(user)
    await db.commit()
    await db.refresh(user)

    raw_token = await create_session(db, user.id)
    set_session_cookie(response, raw_token, request)
    logger.info("Registered user: user_id=%s email=%s", user.id, user.email)
    return user_response(user, session_token=raw_token)


@router.post("/login", response_model=AuthUserResponse)
async def login(
    body: AuthCredentialsRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    if not auth_enabled():
        raise HTTPException(status_code=404, detail="Authentication is disabled.")

    email = normalize_email(body.email)
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    raw_token = await create_session(db, user.id)
    set_session_cookie(response, raw_token, request)
    return user_response(user, session_token=raw_token)


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    if auth_enabled():
        await revoke_session(db, session_token_from_request(request))
    clear_session_cookie(response, request)
    return {"ok": True}


@router.get("/me", response_model=AuthUserResponse)
async def me(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    if not auth_enabled():
        return AuthUserResponse(user_id="", email="", auth_enabled=False, is_admin=False)

    user = await resolve_user_from_request(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return user_response(user)

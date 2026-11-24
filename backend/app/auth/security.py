"""Authentication, sessions, CSRF, RBAC. Tenant ALWAYS comes from the server-side session, never the body."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import Forbidden, TooManyRequests, Unauthenticated
from app.db.base import SessionLocal, utcnow
from app.db.models_core import LoginAttempt, SessionRow, User

COOKIE = "aether_session"
CSRF_HEADER = "x-csrf-token"
ROLES = ("viewer", "maker", "checker", "compliance_officer", "auditor", "tenant_admin", "service")
_ph = PasswordHasher()  # Argon2id defaults


def hash_password(pw: str) -> str:
    return _ph.hash(pw)


def verify_password(h: str, pw: str) -> bool:
    try:
        return _ph.verify(h, pw)
    except (VerificationError, InvalidHashError):
        return False


def _th(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def get_db():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


@dataclass(frozen=True)
class Principal:
    user_id: str
    tenant_id: str
    username: str
    display_name: str
    roles: tuple[str, ...]
    entitlements: tuple[str, ...]
    csrf_token: str

    def has(self, *roles: str) -> bool:
        return any(r in self.roles for r in roles)


def _attempt_key(tenant_id: str, username: str, ip: str) -> str:
    return f"{tenant_id}|{username.lower()}|{ip}"


def login(db: Session, tenant_id: str, username: str, password: str, ip: str) -> tuple[str, Principal]:
    st = get_settings()
    key = _attempt_key(tenant_id, username, ip)
    since = utcnow() - timedelta(seconds=st.login_window_seconds)
    fails = db.execute(select(func.count()).select_from(LoginAttempt).where(
        LoginAttempt.key == key, LoginAttempt.at >= since, LoginAttempt.success.is_(False))).scalar_one()
    if fails >= st.login_max_failures:
        raise TooManyRequests("Too many failed sign-in attempts; try again later")
    user = db.execute(select(User).where(User.tenant_id == tenant_id, User.username == username)).scalar_one_or_none()
    ok = bool(user and user.active and verify_password(user.password_hash, password))
    db.add(LoginAttempt(key=key, success=ok))
    if not ok:
        db.commit()  # persist the failure even though we raise
        raise Unauthenticated("Invalid credentials")
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    db.add(SessionRow(token_hash=_th(token), user_id=user.user_id, tenant_id=user.tenant_id, csrf_token=csrf,
                      expires_at=utcnow() + timedelta(seconds=st.session_ttl_seconds)))
    return token, Principal(user.user_id, user.tenant_id, user.username, user.display_name,
                            tuple(user.roles), tuple(user.entitlements or []), csrf)


def logout(db: Session, token: str) -> None:
    row = db.get(SessionRow, _th(token))
    if row:
        row.revoked = True


def current_principal(request: Request, db: Session = Depends(get_db)) -> Principal:
    token = request.cookies.get(COOKIE)
    if not token:
        raise Unauthenticated("Not signed in")
    row = db.get(SessionRow, _th(token))
    if row is None or row.revoked or row.expires_at <= utcnow():
        raise Unauthenticated("Session expired or invalid")
    user = db.get(User, row.user_id)
    if user is None or not user.active:
        raise Unauthenticated("Account inactive")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        sent = request.headers.get(CSRF_HEADER, "")
        if not hmac.compare_digest(sent, row.csrf_token):
            raise Forbidden("CSRF token missing or invalid", code="CSRF_FAILED")
    p = Principal(user.user_id, user.tenant_id, user.username, user.display_name,
                  tuple(user.roles), tuple(user.entitlements or []), row.csrf_token)
    request.state.principal = p
    return p


def require_roles(*roles: str):
    """Deny by default: caller must hold at least one listed role (tenant_admin does NOT imply approver)."""
    def dep(p: Principal = Depends(current_principal)) -> Principal:
        if not p.has(*roles):
            raise Forbidden(f"Requires one of roles: {', '.join(roles)}")
        return p
    return dep


def require_distinct_checker(maker_id: str, checker: Principal) -> None:
    """Maker-checker: nobody (including admins) approves their own material work."""
    if checker.user_id == maker_id:
        raise Forbidden("Maker cannot approve their own work (independent checker required)", code="SELF_APPROVAL")
    if not checker.has("checker", "compliance_officer"):
        raise Forbidden("Checker role required", code="CHECKER_ROLE_REQUIRED")


def new_password() -> str:
    return secrets.token_urlsafe(14)

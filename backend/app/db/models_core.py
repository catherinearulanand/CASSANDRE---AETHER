"""Shared tables owned by the lead: tenancy, identity, sessions, idempotency, outbox/inbox, runs."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UTCDateTime, new_id, utcnow


class Tenant(Base):
    __tablename__ = "core_tenants"
    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class User(Base):
    __tablename__ = "core_users"
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("core_tenants.tenant_id"), index=True)
    username: Mapped[str] = mapped_column(String(100))
    display_name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(255))
    roles: Mapped[list] = mapped_column(JSON)  # list[str]
    entitlements: Mapped[list] = mapped_column(JSON, default=list)  # list[str], e.g. market scopes
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("tenant_id", "username", name="uq_user_tenant_username"),)


class SessionRow(Base):
    __tablename__ = "core_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256 of opaque token
    user_id: Mapped[str] = mapped_column(ForeignKey("core_users.user_id"), index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class LoginAttempt(Base):
    __tablename__ = "core_login_attempts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(300), index=True)  # tenant|username|ip
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    success: Mapped[bool] = mapped_column(Boolean)


class IdempotencyRecord(Base):
    __tablename__ = "core_idempotency"
    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    scope: Mapped[str] = mapped_column(String(100), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict] = mapped_column(JSON)
    status_code: Mapped[int] = mapped_column(Integer, default=200)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class OutboxEvent(Base):
    """Transactional outbox: written in the same transaction as the domain change."""
    __tablename__ = "core_outbox"
    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    envelope: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)  # PENDING|DELIVERED|DEAD
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class InboxRecord(Base):
    """Consumer idempotency: unique (consumer, event_id) => at-least-once transport, idempotent effects."""
    __tablename__ = "core_inbox"
    consumer: Mapped[str] = mapped_column(String(100), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Run(Base):
    __tablename__ = "core_runs"
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    parent_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scenario_id: Mapped[str] = mapped_column(String(80))
    scenario_revision: Mapped[str] = mapped_column(String(32), default="1")
    market: Mapped[str] = mapped_column(String(24))  # OTC | ETD_DOMESTIC | ETD_IFSC
    mode: Mapped[str] = mapped_column(String(16), default="REPLAY")
    as_of: Mapped[datetime] = mapped_column(UTCDateTime)
    seed: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[str] = mapped_column(String(24), default="CREATED", index=True)
    block_reasons: Mapped[list] = mapped_column(JSON, default=list)
    input_manifest: Mapped[dict] = mapped_column(JSON, default=dict)
    output_manifest: Mapped[dict] = mapped_column(JSON, default=dict)
    engine_versions: Mapped[dict] = mapped_column(JSON, default=dict)
    correlation_id: Mapped[str] = mapped_column(String(64), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    __table_args__ = (Index("ix_runs_tenant_created", "tenant_id", "created_at"),)


class ApprovalRow(Base):
    """Generic maker-checker approval record. Binds exact target revision + hash, expires."""
    __tablename__ = "core_approvals"
    approval_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    action_type: Mapped[str] = mapped_column(String(60))
    target_type: Mapped[str] = mapped_column(String(60))
    target_id: Mapped[str] = mapped_column(String(64), index=True)
    target_revision: Mapped[int] = mapped_column(Integer)
    target_hash: Mapped[str] = mapped_column(String(64))
    maker_id: Mapped[str] = mapped_column(String(64))
    checker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision: Mapped[str] = mapped_column(String(16), default="PENDING")  # PENDING|APPROVED|REJECTED|EXPIRED
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    revision: Mapped[int] = mapped_column(Integer, default=1)

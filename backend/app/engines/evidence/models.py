"""A6 VERITAS tables (prefix ev_). Lead supplies the append foundation; A6 owns and extends this file."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UTCDateTime, utcnow


class EvChainHead(Base):
    __tablename__ = "ev_chain_heads"
    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, default=0)  # last used sequence (0 = empty chain)
    head_hash: Mapped[str] = mapped_column(String(64), default="0" * 64)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class EvEvent(Base):
    __tablename__ = "ev_events"
    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(64))
    engine: Mapped[str] = mapped_column(String(24))  # regulatory|contracts|risk|collateral|compliance|evidence|platform
    action: Mapped[str] = mapped_column(String(80), index=True)
    actor_id: Mapped[str] = mapped_column(String(64))
    actor_type: Mapped[str] = mapped_column(String(16))  # USER|SERVICE|MODEL|SYSTEM
    actor_role: Mapped[str | None] = mapped_column(String(40), nullable=True)
    service_version: Mapped[str] = mapped_column(String(60))
    correlation_id: Mapped[str] = mapped_column(String(64), index=True)
    causation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_type: Mapped[str] = mapped_column(String(60))
    entity_id: Mapped[str] = mapped_column(String(80), index=True)
    entity_revision: Mapped[int] = mapped_column(Integer, default=1)
    effective_at: Mapped[datetime] = mapped_column(UTCDateTime)  # business time
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime)  # recording time (bitemporal)
    source_mode: Mapped[str] = mapped_column(String(16), default="REPLAY")
    input_hashes: Mapped[list] = mapped_column(JSON, default=list)
    source_spans: Mapped[list] = mapped_column(JSON, default=list)
    output_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64))
    event_hash: Mapped[str] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("tenant_id", "event_id", name="uq_ev_tenant_event"),
                      Index("ix_ev_tenant_recorded", "tenant_id", "recorded_at"),
                      Index("ix_ev_tenant_actor", "tenant_id", "actor_id"))


class EvObject(Base):
    """Content-addressed object index. Bytes live in the object store (path derived from hash), never updated."""
    __tablename__ = "ev_objects"
    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    classification: Mapped[str] = mapped_column(String(32))  # e.g. SOURCE_DOCUMENT, SNAPSHOT, EXPORT_BUNDLE, TEMP
    size_bytes: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class EvLineage(Base):
    """Explicit provenance edges recorded by engines (parent -> child). Also derived from event payload.parents."""
    __tablename__ = "ev_lineage"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    correlation_id: Mapped[str] = mapped_column(String(64), index=True)
    parent_type: Mapped[str] = mapped_column(String(60))
    parent_id: Mapped[str] = mapped_column(String(80))
    child_type: Mapped[str] = mapped_column(String(60))
    child_id: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("tenant_id", "parent_type", "parent_id", "child_type", "child_id",
                                       name="uq_ev_lineage_edge"),)


class EvCheckpoint(Base):
    """Signed chain-head checkpoint. The DB copy is a convenience; the TRUSTED copy is the exported file
    outside the DB write boundary plus the public key held by the verifier."""
    __tablename__ = "ev_checkpoints"
    checkpoint_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    head_hash: Mapped[str] = mapped_column(String(64))
    key_id: Mapped[str] = mapped_column(String(32))
    signature: Mapped[str] = mapped_column(String(200))  # base64 Ed25519
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    exported_path: Mapped[str | None] = mapped_column(String(500), nullable=True)


class EvExport(Base):
    __tablename__ = "ev_exports"
    export_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    actor_id: Mapped[str] = mapped_column(String(64))
    redacted: Mapped[bool] = mapped_column(Boolean, default=False)
    bundle_hash: Mapped[str] = mapped_column(String(64))
    manifest_hash: Mapped[str] = mapped_column(String(64))
    checkpoint_id: Mapped[str] = mapped_column(String(64))
    event_count: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="READY")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class EvReplay(Base):
    __tablename__ = "ev_replays"
    replay_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    actor_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24))  # MATCH | MATCH_WITHIN_TOLERANCE | DIVERGED | NOT_REPLAYABLE | NOTHING_TO_REPLAY
    result: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class EvAccessLog(Base):
    """Audit-access log. Deliberately NOT written into the evidence chain (avoids recursive self-audit loops)."""
    __tablename__ = "ev_access_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    actor_id: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(60))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class EvRetention(Base):
    """Retention config per data category. retention_days NULL = not configured (no statutory duration is invented)."""
    __tablename__ = "ev_retention"
    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    category: Mapped[str] = mapped_column(String(40), primary_key=True)
    retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    legal_hold: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

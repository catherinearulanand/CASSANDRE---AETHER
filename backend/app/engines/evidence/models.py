"""A6 VERITAS tables (prefix ev_). Lead supplies the append foundation; A6 owns and extends this file."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Integer, String, Text, UniqueConstraint
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
    __table_args__ = (UniqueConstraint("tenant_id", "event_id", name="uq_ev_tenant_event"),)

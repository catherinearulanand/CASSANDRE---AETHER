"""A3 VECTOR tables (prefix risk_). Inputs are immutable once pinned (content hash enforced in services)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CommonColumns, UTCDateTime, new_id


class RiskSnapshot(Base, CommonColumns):
    __tablename__ = "risk_snapshots"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    snapshot_id: Mapped[str] = mapped_column(String(80))
    data_as_of: Mapped[datetime] = mapped_column(UTCDateTime)
    source_mode: Mapped[str] = mapped_column(String(16), default="REPLAY")
    max_age_seconds: Mapped[int] = mapped_column(Integer, default=86400)
    payload: Mapped[dict] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    __table_args__ = (UniqueConstraint("tenant_id", "snapshot_id", name="uq_risk_snapshot"),)


class RiskInstrument(Base, CommonColumns):
    __tablename__ = "risk_instruments"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    instrument_id: Mapped[str] = mapped_column(String(80))
    venue: Mapped[str] = mapped_column(String(24))
    payload: Mapped[dict] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("tenant_id", "instrument_id", name="uq_risk_instrument"),)


class RiskTrade(Base, CommonColumns):
    __tablename__ = "risk_trades"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    trade_id: Mapped[str] = mapped_column(String(80))
    master_id: Mapped[str] = mapped_column(String(80))
    netting_set_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    trade_type: Mapped[str] = mapped_column(String(8))
    maturity: Mapped[str] = mapped_column(String(10))
    payload: Mapped[dict] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("tenant_id", "trade_id", name="uq_risk_trade"),
                      Index("ix_risk_trade_ns", "tenant_id", "netting_set_id"))


class RiskPosition(Base, CommonColumns):
    __tablename__ = "risk_positions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(String(80))
    instrument_id: Mapped[str] = mapped_column(String(80))
    as_of: Mapped[datetime] = mapped_column(UTCDateTime)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    signed_lots: Mapped[str] = mapped_column(String(40))
    settlement_price: Mapped[str] = mapped_column(String(40))
    __table_args__ = (UniqueConstraint("tenant_id", "account_id", "instrument_id", "as_of", "revision", name="uq_risk_position"),)


class RiskClearingFile(Base, CommonColumns):
    __tablename__ = "risk_clearing_files"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    file_id: Mapped[str] = mapped_column(String(100))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    account_id: Mapped[str] = mapped_column(String(80), index=True)
    venue: Mapped[str] = mapped_column(String(24))
    currency: Mapped[str] = mapped_column(String(3))
    as_of: Mapped[datetime] = mapped_column(UTCDateTime)
    rows: Mapped[list] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))
    computed_total: Mapped[str] = mapped_column(String(40))
    reported_total: Mapped[str | None] = mapped_column(String(40), nullable=True)
    status: Mapped[str] = mapped_column(String(16))  # RECONCILED | EXCEPTION
    reconciliation: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("tenant_id", "file_id", "revision", name="uq_risk_clearing_file"),)


class RiskResult(Base, CommonColumns):
    __tablename__ = "risk_results"
    result_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    market_type: Mapped[str] = mapped_column(String(16))
    subject_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(16))
    cache_key: Mapped[str] = mapped_column(String(64), index=True)  # tenant + all input hashes + method version
    result_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    # Cacheable (OK/UNSUPPORTED) results are unique per (tenant, cache_key): concurrent identical computes cannot store two rows.
    # BLOCKED rows are audit records of each attempt and are deliberately NOT unique.
    __table_args__ = (Index("ix_risk_result_tenant_created", "tenant_id", "created_at"),
                      Index("uq_risk_result_cacheable", "tenant_id", "cache_key", unique=True,
                            postgresql_where=text("status IN ('OK','UNSUPPORTED')"), sqlite_where=text("status IN ('OK','UNSUPPORTED')")))


class RiskStressRun(Base, CommonColumns):
    __tablename__ = "risk_stress_runs"
    stress_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    base_result_id: Mapped[str] = mapped_column(String(64), index=True)
    shock_hash: Mapped[str] = mapped_column(String(64))
    shock_spec: Mapped[dict] = mapped_column(JSON)
    payload: Mapped[dict] = mapped_column(JSON)

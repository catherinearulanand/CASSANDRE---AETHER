"""A4 ATLAS tables (prefix col_).

Quantities are stored as scaled integers ("micro-units", 1 unit = 10**6 u) so that inventory arithmetic
(available = units - encumbered - reserved) is exact and can be enforced by a conditional SQL UPDATE on
both SQLite and PostgreSQL. Prices / rates / amounts that are never used in SQL arithmetic are decimal strings.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, BigInteger, Boolean, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UTCDateTime, new_id, utcnow

SCALE = 10**6  # micro-units


class ColSnapshot(Base):
    __tablename__ = "col_snapshots"
    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(16), default="1")
    provenance: Mapped[str] = mapped_column(String(24), default="SYNTHETIC")
    source_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    as_of: Mapped[datetime] = mapped_column(UTCDateTime)
    currency: Mapped[str] = mapped_column(String(3))  # posting/obligation currency of this pool (no blending)
    market_type: Mapped[str] = mapped_column(String(16))
    content_hash: Mapped[str] = mapped_column(String(64))
    policy: Mapped[dict] = mapped_column(JSON, default=dict)  # clearing/posting policy schedule (synthetic)
    wrong_way_issuer_groups: Mapped[list] = mapped_column(JSON, default=list)  # explicit relationships, never name similarity
    lot_count: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ColLot(Base):
    __tablename__ = "col_inventory"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    snapshot_id: Mapped[str] = mapped_column(String(80))
    lot_id: Mapped[str] = mapped_column(String(80))
    owner_entity: Mapped[str] = mapped_column(String(64))
    custody_account: Mapped[str] = mapped_column(String(80))
    custodian: Mapped[str | None] = mapped_column(String(80), nullable=True)
    issuer: Mapped[str | None] = mapped_column(String(80), nullable=True)
    issuer_group: Mapped[str | None] = mapped_column(String(80), nullable=True)
    issuer_class: Mapped[str | None] = mapped_column(String(40), nullable=True)
    asset_class: Mapped[str] = mapped_column(String(40))  # CASH | GSEC | CORP | ...
    currency: Mapped[str] = mapped_column(String(3))
    units_u: Mapped[int] = mapped_column(BigInteger)  # free units held
    encumbered_u: Mapped[int] = mapped_column(BigInteger, default=0)
    reserved_u: Mapped[int] = mapped_column(BigInteger, default=0)
    lot_size_u: Mapped[int] = mapped_column(BigInteger, default=0)  # 0 = divisible
    price: Mapped[str | None] = mapped_column(String(40), nullable=True)  # lot currency per unit
    fx_to_target: Mapped[str | None] = mapped_column(String(40), nullable=True)  # target ccy per 1 lot ccy
    rating: Mapped[str | None] = mapped_column(String(8), nullable=True)
    maturity_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    annual_cost_rate: Mapped[str | None] = mapped_column(String(40), nullable=True)  # decimal FRACTION p.a.
    txn_cost_per_unit: Mapped[str] = mapped_column(String(40), default="0")  # target ccy per unit
    settlement_lag_days: Mapped[int] = mapped_column(Integer, default=0)
    reuse_permission: Mapped[bool] = mapped_column(Boolean, default=False)
    entitled_to: Mapped[list] = mapped_column(JSON, default=list)  # obligation ids this lot may cover (default deny)
    posted_account_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    posted_u: Mapped[int] = mapped_column(BigInteger, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (UniqueConstraint("tenant_id", "snapshot_id", "lot_id", name="uq_col_lot"),
                      Index("ix_col_lot_snap", "tenant_id", "snapshot_id"))


class ColProposal(Base):
    __tablename__ = "col_proposals"
    proposal_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(10), default="PROPOSAL")  # PROPOSAL | STRESS (never approvable)
    state: Mapped[str] = mapped_column(String(16), index=True)
    solver_status: Mapped[str] = mapped_column(String(24))
    hypothetical: Mapped[bool] = mapped_column(Boolean, default=False)
    market_type: Mapped[str] = mapped_column(String(16))
    obligation_id: Mapped[str] = mapped_column(String(80), index=True)
    margin_result_id: Mapped[str] = mapped_column(String(80))
    margin_result_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    inventory_snapshot_id: Mapped[str] = mapped_column(String(80))
    inventory_hash: Mapped[str] = mapped_column(String(64))
    terms_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rule_set_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deadline: Mapped[datetime] = mapped_column(UTCDateTime)
    as_of: Mapped[datetime] = mapped_column(UTCDateTime)
    horizon_days: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    required_coverage: Mapped[str] = mapped_column(String(48))  # effective coverage still to be posted
    objective: Mapped[str | None] = mapped_column(String(48), nullable=True)  # horizon cost, target ccy
    residual: Mapped[str | None] = mapped_column(String(48), nullable=True)
    baseline: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)  # solver info, exclusions, diagnostics, checks
    params: Mapped[dict] = mapped_column(JSON, default=dict)  # frozen policy used (verifier input)
    proposal_hash: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64), index=True)
    approval_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    simulated_settlement: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    __table_args__ = (Index("ix_col_prop_tenant_created", "tenant_id", "created_at"),)


class ColLeg(Base):
    __tablename__ = "col_legs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    proposal_id: Mapped[str] = mapped_column(String(64), index=True)
    lot_id: Mapped[str] = mapped_column(String(80))
    asset_class: Mapped[str] = mapped_column(String(40))
    currency: Mapped[str] = mapped_column(String(3))
    quantity: Mapped[str] = mapped_column(String(48))
    price: Mapped[str] = mapped_column(String(40))
    fx_rate: Mapped[str] = mapped_column(String(40))
    gross_value: Mapped[str] = mapped_column(String(48))  # target ccy, before haircuts
    valuation_factor: Mapped[str] = mapped_column(String(24))
    fx_factor: Mapped[str] = mapped_column(String(24))
    effective_coverage: Mapped[str] = mapped_column(String(48))
    cost_horizon: Mapped[str] = mapped_column(String(48))
    share_of_coverage: Mapped[str] = mapped_column(String(24))
    settlement_lag_days: Mapped[int] = mapped_column(Integer, default=0)


class ColConstraint(Base):
    __tablename__ = "col_constraints"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    proposal_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(24))  # COVERAGE | CAP_TOTAL | CAP_OBLIGATION | AVAILABILITY | OVERPOST
    limit_value: Mapped[str] = mapped_column(String(48))
    measured_value: Mapped[str] = mapped_column(String(48))
    slack: Mapped[str] = mapped_column(String(48))
    binding: Mapped[bool] = mapped_column(Boolean, default=False)
    unit: Mapped[str] = mapped_column(String(40), default="")


class ColReservation(Base):
    __tablename__ = "col_reservations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    proposal_id: Mapped[str] = mapped_column(String(64), index=True)
    lot_pk: Mapped[str] = mapped_column(String(64))
    lot_id: Mapped[str] = mapped_column(String(80))
    quantity_u: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(12), default="ACTIVE")  # ACTIVE | RELEASED | SETTLED_SIM
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    released_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    __table_args__ = (UniqueConstraint("tenant_id", "proposal_id", "lot_pk", name="uq_col_reservation"),)


class ColInstructionDraft(Base):
    __tablename__ = "col_instruction_drafts"
    proposal_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(20), default="NOT_TRANSMITTED")
    content: Mapped[dict] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

"""A2 COVENANT tables (prefix con_). Only this engine writes them; others use contracts.* services."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CommonColumns, UTCDateTime, new_id, utcnow


class ConPackage(CommonColumns, Base):
    __tablename__ = "con_packages"
    package_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    master_id: Mapped[str] = mapped_column(String(80))
    csa_id: Mapped[str] = mapped_column(String(80))
    netting_set_id: Mapped[str] = mapped_column(String(80))
    party_a_id: Mapped[str] = mapped_column(String(80))
    party_b_id: Mapped[str] = mapped_column(String(80))
    party_a_name: Mapped[str] = mapped_column(String(200))
    party_b_name: Mapped[str] = mapped_column(String(200))
    base_currency: Mapped[str] = mapped_column(String(3))
    governing_law: Mapped[str] = mapped_column(String(200))
    doc_type: Mapped[str] = mapped_column(String(40), default="BILATERAL_VM_CSA")
    status: Mapped[str] = mapped_column(String(24), default="DRAFT")  # DRAFT|IN_REVIEW|FROZEN|AMENDMENT_IN_REVIEW
    revision: Mapped[int] = mapped_column(Integer, default=1)  # bumped by every field change (invalidates approvals)
    created_by: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    __table_args__ = (
        UniqueConstraint("tenant_id", "netting_set_id", name="uq_con_pkg_tenant_netting_set"),
        UniqueConstraint("tenant_id", "master_id", "netting_set_id", name="uq_con_pkg_tenant_master_ns"),
        Index("ix_con_pkg_tenant_status", "tenant_id", "status"),
    )


class ConDocument(CommonColumns, Base):
    __tablename__ = "con_documents"
    doc_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    package_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(300))
    doc_ref: Mapped[str] = mapped_column(String(120))
    sha256: Mapped[str] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text)  # immutable original text (never edited)
    effective_from: Mapped[datetime] = mapped_column(UTCDateTime)
    precedence_rank: Mapped[int] = mapped_column(Integer)
    missing_annexes: Mapped[list] = mapped_column(JSON, default=list)
    extracted_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_by: Mapped[str] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("package_id", "sha256", name="uq_con_doc_pkg_sha"),)


class ConField(CommonColumns, Base):
    __tablename__ = "con_fields"
    field_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    package_id: Mapped[str] = mapped_column(String(64), index=True)
    doc_id: Mapped[str] = mapped_column(String(64))
    field_key: Mapped[str] = mapped_column(String(60))
    direction: Mapped[str | None] = mapped_column(String(1), nullable=True)
    value: Mapped[dict | list | str | bool | None] = mapped_column(JSON, nullable=True)
    original_value: Mapped[dict | list | str | bool | None] = mapped_column(JSON, nullable=True)  # extraction, never edited
    quote: Mapped[str] = mapped_column(Text)
    start_char: Mapped[int] = mapped_column(Integer)
    end_char: Mapped[int] = mapped_column(Integer)
    condition: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    effective_from: Mapped[datetime] = mapped_column(UTCDateTime)
    rank: Mapped[int] = mapped_column(Integer)
    extraction_mode: Mapped[str] = mapped_column(String(16))  # RULES | REPLAY | MANUAL
    status: Mapped[str] = mapped_column(String(16), default="PROPOSED")  # PROPOSED|UNRESOLVED|APPROVED|REJECTED
    issues: Mapped[list] = mapped_column(JSON, default=list)
    maker_id: Mapped[str] = mapped_column(String(64))
    reviewer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    frozen_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    __table_args__ = (Index("ix_con_field_pkg_key", "package_id", "field_key"),)


class ConVersion(CommonColumns, Base):
    """Immutable frozen terms bundle. No code path updates a row except marking status SUPERSEDED."""
    __tablename__ = "con_versions"
    version_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    package_id: Mapped[str] = mapped_column(String(64), index=True)
    master_id: Mapped[str] = mapped_column(String(80))
    netting_set_id: Mapped[str] = mapped_column(String(80))
    version_no: Mapped[int] = mapped_column(Integer)
    bundle_hash: Mapped[str] = mapped_column(String(64))
    entries: Mapped[list] = mapped_column(JSON)  # effective-dated approved entries
    pkg_static: Mapped[dict] = mapped_column(JSON)  # parties, base currency
    source_graph: Mapped[dict] = mapped_column(JSON)
    required_facts: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(12), default="FROZEN")  # FROZEN | SUPERSEDED
    approval_id: Mapped[str] = mapped_column(String(64))
    maker_id: Mapped[str] = mapped_column(String(64))
    checker_id: Mapped[str] = mapped_column(String(64))
    frozen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    supersedes_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    __table_args__ = (UniqueConstraint("tenant_id", "master_id", "netting_set_id", "version_no", name="uq_con_ver_revision"),)


class ConEligibility(CommonColumns, Base):
    """Row-level view of an eligible-schedule field (rebuilt on extraction/correction; the field is authoritative)."""
    __tablename__ = "con_eligibility"
    row_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    package_id: Mapped[str] = mapped_column(String(64), index=True)
    field_id: Mapped[str] = mapped_column(String(64), index=True)
    asset_class: Mapped[str] = mapped_column(String(40))
    currency: Mapped[str] = mapped_column(String(3))
    issuer_class: Mapped[str] = mapped_column(String(40))
    valuation_pct: Mapped[str] = mapped_column(String(20))
    haircut_pct: Mapped[str] = mapped_column(String(20))
    fx_haircut_pct: Mapped[str] = mapped_column(String(20))
    concentration_cap_pct: Mapped[str | None] = mapped_column(String(20), nullable=True)
    cap_denominator: Mapped[str | None] = mapped_column(String(32), nullable=True)
    min_rating: Mapped[str | None] = mapped_column(String(8), nullable=True)
    max_maturity_years: Mapped[str | None] = mapped_column(String(20), nullable=True)
    field_status: Mapped[str] = mapped_column(String(16))


class ConNettingMembership(CommonColumns, Base):
    __tablename__ = "con_netting_membership"
    membership_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    trade_id: Mapped[str] = mapped_column(String(120))
    declared_master_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    declared_netting_set_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    netting_set_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    master_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    status: Mapped[str] = mapped_column(String(16))  # MEMBER | QUARANTINED | REJECTED
    reason: Mapped[str] = mapped_column(Text)
    recorded_by: Mapped[str] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("tenant_id", "trade_id", name="uq_con_member_tenant_trade"),
                      Index("ix_con_member_ns", "tenant_id", "netting_set_id"))


class ConFactSnapshot(CommonColumns, Base):
    """External facts needed by conditional terms (rating, downgrade...). Each fact needs an explicit source."""
    __tablename__ = "con_fact_snapshots"
    row_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    fact_snapshot_id: Mapped[str] = mapped_column(String(80))  # unique per tenant, not globally
    facts: Mapped[dict] = mapped_column(JSON)  # key -> {value, source_id, effective_at}
    facts_hash: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("tenant_id", "fact_snapshot_id", name="uq_con_fact_tenant_id"),)


class ConResolution(Base):
    """Resolved terms keyed by terms_hash so other engines can re-read exactly what was pinned."""
    __tablename__ = "con_resolutions"
    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    terms_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    netting_set_id: Mapped[str] = mapped_column(String(80))
    version_id: Mapped[str] = mapped_column(String(64))
    as_of: Mapped[datetime] = mapped_column(UTCDateTime)
    fact_snapshot_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    terms: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

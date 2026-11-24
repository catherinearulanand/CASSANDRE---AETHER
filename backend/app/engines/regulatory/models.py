"""A1 REGULUS tables (prefix reg_). Owned by A1."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, ForeignKey, Index, Integer, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CommonColumns, UTCDateTime, new_id, utcnow

AUTHORITY_TYPES = ("AUTHORITATIVE", "DRAFT_CONSULTATION", "INDUSTRY_STANDARD", "INTERNAL_POLICY", "SYNTHETIC", "SUPERSEDED")
RULE_AUTHORITY = ("SYNTHETIC_POLICY", "INTERNAL_POLICY", "REGULATORY_VERIFIED")
RULE_STATES = ("DRAFT", "REVIEW_REQUIRED", "APPROVED", "SCHEDULED", "ACTIVE", "SUPERSEDED", "WITHDRAWN")


class RegSource(Base, CommonColumns):
    __tablename__ = "reg_sources"
    source_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    authority_type: Mapped[str] = mapped_column(String(24), index=True)
    doc_kind: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(300))
    issuer: Mapped[str | None] = mapped_column(String(200), nullable=True)
    doc_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    official_uri: Mapped[str | None] = mapped_column(String(500), nullable=True)  # None for synthetic docs
    published_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    retrieved_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    superseded_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    classification: Mapped[str] = mapped_column(String(40), default="SYNTHETIC")
    verification: Mapped[str] = mapped_column(String(20), default="UNVERIFIED")  # public sources stay UNVERIFIED until reviewed
    kind: Mapped[str] = mapped_column(String(8))  # pdf | html | text
    sha256: Mapped[str] = mapped_column(String(64))
    raw_bytes: Mapped[bytes] = mapped_column(LargeBinary)  # immutable original
    text: Mapped[str] = mapped_column(Text)  # extracted text stored separately; pages joined by \f
    text_sha256: Mapped[str] = mapped_column(String(64))
    page_count: Mapped[int] = mapped_column(Integer, default=1)
    unreadable_pages: Mapped[list] = mapped_column(JSON, default=list)
    parser_version: Mapped[str] = mapped_column(String(60))
    risk_flags: Mapped[list] = mapped_column(JSON, default=list)
    created_by: Mapped[str] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("tenant_id", "sha256", name="uq_reg_source_hash"),
                      Index("ix_reg_sources_tenant_auth", "tenant_id", "authority_type"))


class RegSpan(Base, CommonColumns):
    __tablename__ = "reg_spans"
    span_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(ForeignKey("reg_sources.source_id"), index=True)
    page: Mapped[int] = mapped_column(Integer)
    start: Mapped[int] = mapped_column(Integer)  # character offsets into RegSource.text
    end: Mapped[int] = mapped_column(Integer)
    byte_start: Mapped[int | None] = mapped_column(Integer, nullable=True)  # utf-8 byte offsets (text docs only)
    byte_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quote: Mapped[str] = mapped_column(Text)
    method: Mapped[str] = mapped_column(String(24))  # REPLAY | DETERMINISTIC | MANUAL | LIVE_MODEL
    created_by: Mapped[str] = mapped_column(String(64))


class RegCandidate(Base, CommonColumns):
    __tablename__ = "reg_candidates"
    candidate_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(ForeignKey("reg_sources.source_id"), index=True)
    mode: Mapped[str] = mapped_column(String(16))
    fields: Mapped[dict] = mapped_column(JSON)
    span_ids: Mapped[list] = mapped_column(JSON, default=list)
    issues: Mapped[list] = mapped_column(JSON, default=list)
    grounded: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence_category: Mapped[str] = mapped_column(String(24))  # NOT an accuracy claim
    status: Mapped[str] = mapped_column(String(16), default="PROPOSED")
    run_hash: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(64))


class RegRule(Base, CommonColumns):
    __tablename__ = "reg_rules"
    rule_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    rule_key: Mapped[str] = mapped_column(String(80))
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")
    authority_type: Mapped[str] = mapped_column(String(24))
    title: Mapped[str] = mapped_column(String(300), default="")
    scope: Mapped[dict] = mapped_column(JSON)
    condition: Mapped[dict] = mapped_column(JSON)
    parameters: Mapped[dict] = mapped_column(JSON, default=dict)
    exceptions: Mapped[list] = mapped_column(JSON, default=list)
    span_ids: Mapped[list] = mapped_column(JSON, default=list)
    markets: Mapped[list] = mapped_column(JSON, default=list)
    conflict_group: Mapped[str | None] = mapped_column(String(80), nullable=True)
    precedence_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)  # lower wins; human-approved content
    additive_stricter: Mapped[bool] = mapped_column(Boolean, default=False)
    effective_from: Mapped[datetime] = mapped_column(UTCDateTime)
    effective_to: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)  # recording time of activation
    withdrawn_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    superseded_by_rule_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    supersedes_rule_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    approved_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    compile_report: Mapped[dict] = mapped_column(JSON, default=dict)
    review_notes: Mapped[list] = mapped_column(JSON, default=list)
    rationale: Mapped[str] = mapped_column(Text, default="")
    candidate_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    maker_id: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("tenant_id", "rule_key", "version", name="uq_reg_rule_key_version"),
                      Index("ix_reg_rules_tenant_key", "tenant_id", "rule_key", "version"),
                      Index("ix_reg_rules_tenant_eff", "tenant_id", "effective_from"),
                      Index("ix_reg_rules_tenant_auth", "tenant_id", "authority_type", "status"))


class RegRuleTest(Base, CommonColumns):
    __tablename__ = "reg_rule_tests"
    test_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    rule_id: Mapped[str] = mapped_column(ForeignKey("reg_rules.rule_id"), index=True)
    rule_revision: Mapped[int] = mapped_column(Integer)
    vector: Mapped[dict] = mapped_column(JSON)  # {name, context, expect_scope, expect_condition}
    last_result: Mapped[dict] = mapped_column(JSON, default=dict)
    passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


class RegRuleSet(Base, CommonColumns):
    """Immutable pinned snapshot of a resolved rule set (so rule_parameters(rule_set_hash) is reproducible)."""
    __tablename__ = "reg_rule_sets"
    rule_set_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    members: Mapped[list] = mapped_column(JSON)
    hypothetical: Mapped[bool] = mapped_column(Boolean, default=False)


class RegImpactRun(Base, CommonColumns):
    __tablename__ = "reg_impact_runs"
    impact_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    status: Mapped[str] = mapped_column(String(16))  # COMPLETED | PARTIAL | BLOCKED
    hypothetical: Mapped[bool] = mapped_column(Boolean, default=True)
    params: Mapped[dict] = mapped_column(JSON)
    results: Mapped[dict] = mapped_column(JSON)
    created_by: Mapped[str] = mapped_column(String(64))
    __table_args__ = (Index("ix_reg_impact_tenant_created", "tenant_id", "created_at"),)

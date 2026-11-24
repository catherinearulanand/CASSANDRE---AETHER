"""A5 SENTINEL tables (prefix cmp_). Owned by the compliance engine; no other engine writes here."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CommonColumns, UTCDateTime, new_id, utcnow


class CmpControl(CommonColumns, Base):
    """A control = an approved rule version bound to population, inputs, frequency, SLA and severity policy."""
    __tablename__ = "cmp_controls"
    control_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    control_key: Mapped[str] = mapped_column(String(80))
    family: Mapped[str] = mapped_column(String(40))  # MARGIN_COVERAGE|POSITION_LIMIT|RECONCILIATION|FRESHNESS|AGREEMENT|...
    title: Mapped[str] = mapped_column(String(200))
    rule_key: Mapped[str] = mapped_column(String(80))
    rule_version: Mapped[int] = mapped_column(Integer, default=1)
    rule_hash: Mapped[str] = mapped_column(String(64))
    authority_type: Mapped[str] = mapped_column(String(32))  # SYNTHETIC_POLICY|INTERNAL_POLICY|STATUTORY|REGULATORY_DIRECTION...
    scope: Mapped[dict] = mapped_column(JSON)
    condition: Mapped[dict] = mapped_column(JSON)
    params: Mapped[dict] = mapped_column(JSON, default=dict)  # e.g. required_basis, unit notes
    required_inputs: Mapped[list] = mapped_column(JSON, default=list)
    frequency: Mapped[str] = mapped_column(String(16), default="EVENT")  # EVENT|DAILY
    severity: Mapped[str] = mapped_column(String(10), default="HIGH")  # LOW|MEDIUM|HIGH|CRITICAL (FAIL severity)
    max_staleness_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    support_status: Mapped[str] = mapped_column(String(16), default="SUPPORTED")  # SUPPORTED|UNSUPPORTED
    unsupported_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    effective_from: Mapped[datetime] = mapped_column(UTCDateTime)
    effective_to: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    definition_hash: Mapped[str] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("tenant_id", "control_key", "rule_version", name="uq_cmp_control_rev"),
                      Index("ix_cmp_controls_tenant_active", "tenant_id", "active"))


class CmpEvaluation(CommonColumns, Base):
    __tablename__ = "cmp_evaluations"
    evaluation_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    eval_key: Mapped[str] = mapped_column(String(64))
    control_id: Mapped[str] = mapped_column(String(64))
    control_key: Mapped[str] = mapped_column(String(80))
    rule_key: Mapped[str] = mapped_column(String(80))
    rule_version: Mapped[int] = mapped_column(Integer)
    rule_hash: Mapped[str] = mapped_column(String(64))
    subject_type: Mapped[str] = mapped_column(String(20))
    subject_id: Mapped[str] = mapped_column(String(80))
    market_type: Mapped[str] = mapped_column(String(24))
    venue: Mapped[str | None] = mapped_column(String(40), nullable=True)
    status: Mapped[str] = mapped_column(String(16))  # PASS|FAIL|UNKNOWN|NOT_APPLICABLE|CONFLICT|ERROR
    severity: Mapped[str] = mapped_column(String(10))
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    trace: Mapped[dict] = mapped_column(JSON, default=dict)
    measured: Mapped[str | None] = mapped_column(String(60), nullable=True)
    limit_value: Mapped[str | None] = mapped_column(String(60), nullable=True)
    gap: Mapped[str | None] = mapped_column(String(60), nullable=True)
    unit: Mapped[str | None] = mapped_column(String(20), nullable=True)
    as_of: Mapped[datetime] = mapped_column(UTCDateTime)
    snapshot_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    rule_set_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    margin_result_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    allocation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    facts: Mapped[dict] = mapped_column(JSON, default=dict)
    fact_provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    facts_hash: Mapped[str] = mapped_column(String(64))
    waiver_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    case_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    is_recheck: Mapped[bool] = mapped_column(Boolean, default=False)
    correlation_id: Mapped[str] = mapped_column(String(64), index=True)
    __table_args__ = (UniqueConstraint("tenant_id", "eval_key", name="uq_cmp_eval_key"),
                      Index("ix_cmp_eval_subject", "tenant_id", "subject_id", "control_key", "as_of"),
                      Index("ix_cmp_eval_status", "tenant_id", "status"))


class CmpCase(CommonColumns, Base):
    __tablename__ = "cmp_cases"
    case_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    occurrence_key: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(24))  # CONTROL_BREACH|DATA_GAP|ANOMALY_INVESTIGATION
    control_key: Mapped[str] = mapped_column(String(80))
    rule_version: Mapped[int] = mapped_column(Integer, default=0)
    family: Mapped[str] = mapped_column(String(40), default="")
    subject_id: Mapped[str] = mapped_column(String(80))
    market_type: Mapped[str] = mapped_column(String(24))
    title: Mapped[str] = mapped_column(String(300))
    state: Mapped[str] = mapped_column(String(24), default="OPEN")
    severity: Mapped[str] = mapped_column(String(10))
    last_status: Mapped[str] = mapped_column(String(16))  # last raw evaluation status (unchanged by case handling)
    owner_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    maker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    opened_as_of: Mapped[datetime] = mapped_column(UTCDateTime)
    last_as_of: Mapped[datetime] = mapped_column(UTCDateTime)
    due_at: Mapped[datetime] = mapped_column(UTCDateTime)
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1)
    reopen_count: Mapped[int] = mapped_column(Integer, default=0)
    first_evaluation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_evaluation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    margin_result_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    allocation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    eval_context: Mapped[dict] = mapped_column(JSON, default=dict)
    remediations: Mapped[list] = mapped_column(JSON, default=list)
    pending_dismissal: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_recheck: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    review_entered_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    waiver_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolution: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    watchlist: Mapped[bool] = mapped_column(Boolean, default=False)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("tenant_id", "occurrence_key", name="uq_cmp_case_occurrence"),
                      Index("ix_cmp_case_queue", "tenant_id", "state", "severity", "due_at"))


class CmpCaseEvent(Base):
    """Immutable case history and comments (insert-only by convention; no update/delete code path exists)."""
    __tablename__ = "cmp_case_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    case_id: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(40))
    actor_id: Mapped[str] = mapped_column(String(64))
    from_state: Mapped[str | None] = mapped_column(String(24), nullable=True)
    to_state: Mapped[str | None] = mapped_column(String(24), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    as_of: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class CmpWaiver(CommonColumns, Base):
    __tablename__ = "cmp_waivers"
    waiver_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    control_key: Mapped[str] = mapped_column(String(80))
    subject_id: Mapped[str] = mapped_column(String(80))
    reason: Mapped[str] = mapped_column(Text)
    valid_from: Mapped[datetime] = mapped_column(UTCDateTime)
    valid_to: Mapped[datetime] = mapped_column(UTCDateTime)
    status: Mapped[str] = mapped_column(String(12), default="PENDING")  # PENDING|ACTIVE|REJECTED
    requested_by: Mapped[str] = mapped_column(String(64))
    approved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approval_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)


class CmpModelVersion(CommonColumns, Base):
    __tablename__ = "cmp_model_versions"
    model_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(60))
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(12), default="ACTIVE")  # ACTIVE|DISABLED (drift / kill-switch)
    seed: Mapped[int] = mapped_column(Integer)
    feature_definitions: Mapped[list] = mapped_column(JSON)
    split: Mapped[dict] = mapped_column(JSON)
    training_data_hash: Mapped[str] = mapped_column(String(64))
    parameters: Mapped[dict] = mapped_column(JSON)
    threshold: Mapped[float] = mapped_column(Float)
    metrics: Mapped[dict] = mapped_column(JSON)
    model_card: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("tenant_id", "name", "version", name="uq_cmp_model_ver"),)


class CmpScore(CommonColumns, Base):
    __tablename__ = "cmp_scores"
    score_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    model_id: Mapped[str] = mapped_column(String(64), index=True)
    split: Mapped[str] = mapped_column(String(12))  # validation|holdout
    account_id: Mapped[str] = mapped_column(String(80))
    obs_day: Mapped[int] = mapped_column(Integer)
    score: Mapped[float] = mapped_column(Float)
    flagged: Mapped[bool] = mapped_column(Boolean)
    top_features: Mapped[list] = mapped_column(JSON)  # transparent feature deviations (robust z), no causal claims
    case_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # NOTE: injected labels are NOT stored here; they live only in the evaluation harness output.


class CmpJob(CommonColumns, Base):
    __tablename__ = "cmp_jobs"
    job_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(30))  # SURVEILLANCE_TRAIN|EVALUATE_BATCH
    status: Mapped[str] = mapped_column(String(12))  # RUNNING|DONE|FAILED
    watermark_as_of: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

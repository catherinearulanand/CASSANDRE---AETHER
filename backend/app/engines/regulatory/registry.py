"""R01-R04: source registry, extraction persistence, rule lifecycle, maker/checker, bitemporal registry, diff.

Lifecycle: DRAFT -> REVIEW_REQUIRED -> APPROVED -> SCHEDULED|ACTIVE -> SUPERSEDED | WITHDRAWN.
Approved content is immutable (approved_hash). Rejection returns to DRAFT (new revision). Rollback = new version.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.auth.security import Principal
from app.core import services
from app.core.canonical import domain_hash, sha256_hex
from app.core.config import get_settings
from app.core.errors import Blocked, Conflict, Forbidden, NotFound, ValidationFailed
from app.core.events import emit
from app.db.base import lock_for_update, new_id, utcnow
from app.db.models_core import ApprovalRow
from app.engines.regulatory import documents as D
from app.engines.regulatory import replay
from app.engines.regulatory import dsl
from app.engines.regulatory.models import RegCandidate, RegRule, RegRuleTest, RegSource, RegSpan

ENGINE = "regulatory"
BASIS_OK = {
    "SYNTHETIC_POLICY": {"SYNTHETIC", "INTERNAL_POLICY"},
    "INTERNAL_POLICY": {"INTERNAL_POLICY", "SYNTHETIC"},
    "REGULATORY_VERIFIED": {"AUTHORITATIVE"},
}
VISIBLE = ("SCHEDULED", "ACTIVE", "SUPERSEDED", "WITHDRAWN")


def _parents(db, r: "RegRule") -> list[dict]:
    """Lineage parents of a rule: the exact source spans (and, via source_spans refs, their documents)."""
    return [{"entity_type": "source_span", "entity_id": s} for s in sorted(r.span_ids or [])]


def _link_approval(db, actor: Principal, corr, r: "RegRule", approval_id: str) -> None:
    """The approval is itself an evidence entity (lineage stage APPROVAL) whose parent is the approved rule revision."""
    _ev(db, actor, "APPROVAL_RECORDED", "approval", approval_id, 1, corr, output_hash=r.content_hash,
        payload={"target": "reg_rule", "rule_id": r.rule_id, "revision": r.revision, "decision": "APPROVED",
                 "parents": [{"entity_type": "reg_rule", "entity_id": r.rule_id}]})


def _ev(db, actor: Principal, action: str, etype: str, eid: str, rev: int = 1, corr: str | None = None, **kw):
    return services.call("evidence.append", db, actor.tenant_id, engine=ENGINE, action=action, actor_id=actor.user_id,
                         actor_type="USER", actor_role=",".join(actor.roles), entity_type=etype, entity_id=eid,
                         entity_revision=rev, correlation_id=corr or new_id(), source_mode=kw.pop("source_mode", "MANUAL"), **kw)


def aware(dt: datetime, name: str = "datetime") -> datetime:
    if dt.tzinfo is None:
        raise ValidationFailed(f"{name} must be timezone-aware (UTC)", field_errors={name: "naive datetime rejected"})
    return dt.astimezone(UTC)


def get_source(db, tenant_id: str, sid: str) -> RegSource:
    s = db.get(RegSource, sid)
    if s is None or s.tenant_id != tenant_id:
        raise NotFound("Source not found")
    return s


def _lock_key(db, tenant_id: str, rule_key: str) -> None:
    """Serialise ALL mutations of one rule key (version numbering, overlap/supersession checks, activation order).
    Lock order is always: every row of the key by version, THEN nothing else -> no deadlock between two mutators."""
    db.execute(lock_for_update(select(RegRule).where(RegRule.tenant_id == tenant_id, RegRule.rule_key == rule_key)
                               .order_by(RegRule.version)).execution_options(populate_existing=True)).scalars().all()


def get_rule(db, tenant_id: str, rid: str, lock: bool = False) -> RegRule:
    if lock:
        # Mutations lock the whole rule key and refresh, so the optimistic-revision / approval-state / overlap checks that
        # follow see committed data (otherwise two checkers can both pass the check concurrently on PostgreSQL).
        peek = db.get(RegRule, rid)
        if peek is None or peek.tenant_id != tenant_id:
            raise NotFound("Rule not found")
        _lock_key(db, tenant_id, peek.rule_key)
        q = lock_for_update(select(RegRule).where(RegRule.rule_id == rid, RegRule.tenant_id == tenant_id))             .execution_options(populate_existing=True)
        r = db.execute(q).scalar_one_or_none()
    else:
        r = db.get(RegRule, rid)
    if r is None or r.tenant_id != tenant_id:
        raise NotFound("Rule not found")
    return r


def _check_rev(obj, expected):
    if expected is not None and obj.revision != expected:
        raise Conflict(f"stale revision: expected {expected}, current {obj.revision}", code="REVISION_CONFLICT")


# ------------------------------------------------------------------ R01 ingestion
def ingest_document(db, actor: Principal, data: bytes, content_type: str | None, meta: dict, corr: str | None = None,
                    created_at_hint: datetime | None = None) -> tuple[RegSource, bool]:
    st = get_settings()
    if not data:
        raise ValidationFailed("empty file")
    if len(data) > st.max_upload_bytes:
        raise ValidationFailed(f"file exceeds {st.max_upload_bytes} bytes")
    if meta.get("authority_type") not in ("AUTHORITATIVE", "DRAFT_CONSULTATION", "INDUSTRY_STANDARD", "INTERNAL_POLICY", "SYNTHETIC", "SUPERSEDED"):
        raise ValidationFailed("authority_type invalid", field_errors={"authority_type": "one of the allowed classes"})
    if not meta.get("title"):
        raise ValidationFailed("title required", field_errors={"title": "required"})
    sha = sha256_hex(data)
    existing = db.execute(select(RegSource).where(RegSource.tenant_id == actor.tenant_id, RegSource.sha256 == sha)).scalar_one_or_none()
    if existing:
        return existing, True  # deduplicate by hash
    kind = D.sniff(data, content_type)
    ex = D.extract_text(data, kind)
    src = RegSource(
        tenant_id=actor.tenant_id, authority_type=meta["authority_type"], doc_kind=meta.get("doc_kind") or "NOTICE",
        title=meta["title"], issuer=meta.get("issuer"), doc_number=meta.get("doc_number"), official_uri=meta.get("official_uri"),
        published_at=meta.get("published_at"), effective_date=meta.get("effective_date"),
        classification=meta.get("classification") or ("SYNTHETIC" if meta["authority_type"] in ("SYNTHETIC", "INTERNAL_POLICY") else "PUBLIC_SOURCE"),
        verification="UNVERIFIED", kind=kind, sha256=sha, raw_bytes=data, text=ex["text"], text_sha256=sha256_hex(ex["text"].encode()),
        page_count=ex["pages"], unreadable_pages=ex["unreadable"], parser_version=D.PARSER_VERSION,
        risk_flags=D.injection_flags(ex["text"]), created_by=actor.user_id, source_ref=meta.get("source_ref"),
        provenance="SYNTHETIC" if meta["authority_type"] in ("SYNTHETIC", "INTERNAL_POLICY") else "PUBLIC_SOURCE")
    if created_at_hint:
        src.retrieved_at = created_at_hint
    try:
        with db.begin_nested():
            db.add(src)
            db.flush()
    except IntegrityError:  # concurrent upload of the same bytes: the other transaction won; deduplicate by hash
        existing = db.execute(select(RegSource).where(RegSource.tenant_id == actor.tenant_id, RegSource.sha256 == sha)).scalar_one_or_none()
        if existing is None:
            raise
        return existing, True
    _ev(db, actor, "SOURCE_INGESTED", "reg_source", src.source_id, corr=corr, input_hashes=[sha], output_hash=src.text_sha256,
        rationale=f"ingested {kind} ({src.authority_type}); unreadable_pages={ex['unreadable']}; injection_flags={len(src.risk_flags)}",
        payload={"title": src.title, "kind": kind, "pages": ex["pages"], "authority_type": src.authority_type})
    emit(db, event_type="DocumentIngested", tenant_id=actor.tenant_id, correlation_id=corr or new_id(), aggregate_id=src.source_id,
         actor_id=actor.user_id, payload={"sha256": sha, "title": src.title})
    return src, False


def make_span(db, actor: Principal, src: RegSource, start: int, end: int, method: str) -> RegSpan:
    if not (0 <= start < end <= len(src.text)):
        raise ValidationFailed("span offsets out of range", field_errors={"start": "invalid"})
    q = src.text[start:end]
    bs, be = D.byte_offsets(src.kind, bytes(src.raw_bytes), src.text, start, end)
    sp = RegSpan(tenant_id=actor.tenant_id, source_id=src.source_id, page=D.page_of(src.text, start), start=start, end=end,
                 byte_start=bs, byte_end=be, quote=q, method=method, created_by=actor.user_id)
    db.add(sp)
    db.flush()
    return sp


# ------------------------------------------------------------------ R02 extraction
def extract(db, actor: Principal, source_id: str, mode: str, manual: list[dict] | None = None, corr: str | None = None) -> list[RegCandidate]:
    src = get_source(db, actor.tenant_id, source_id)
    if mode == "LIVE_MODEL":
        if not D.settings_live_enabled():
            raise Blocked("LIVE_MODEL extraction is disabled in this build (no hosted provider configured); no silent fallback.", code="LIVE_MODEL_DISABLED")
        raise Blocked("LIVE_MODEL adapter is not implemented in this build.", code="LIVE_MODEL_NOT_IMPLEMENTED")
    if mode not in ("REPLAY", "DETERMINISTIC", "MANUAL"):
        raise ValidationFailed("mode must be REPLAY | DETERMINISTIC | MANUAL | LIVE_MODEL", field_errors={"mode": "invalid"})
    if src.authority_type == "SUPERSEDED":
        raise Blocked("Source is classified SUPERSEDED; extraction blocked.", code="SOURCE_SUPERSEDED")
    if src.unreadable_pages:
        raise Blocked(f"Source has unreadable/missing pages {src.unreadable_pages}; extraction blocked (no silent inference).", code="MISSING_SOURCE_PAGE")
    if mode == "REPLAY":
        fx = D.replay_fixture_for(src.sha256)
        if fx is None:
            raise Blocked("No hand-labelled REPLAY fixture exists for these exact source bytes.", code="NO_REPLAY_FIXTURE")
        raw = [{"quote": c["quote"], "fields": c["fields"], "issues": list(c.get("issues", []))} for c in fx["candidates"]]
    elif mode == "MANUAL":
        raw = [dict(D.validate_manual_candidate(c), issues=[]) for c in (manual or [])]
        if not raw:
            raise ValidationFailed("MANUAL mode needs at least one candidate")
    else:
        raw = D.deterministic_candidates(src.text)
        for c in raw:
            c["_start"] = c.pop("start")
    run_hash = domain_hash("aether.reg.extract.v1", {"source": src.sha256, "mode": mode, "n": len(raw), "parser": D.PARSER_VERSION})
    made: list[dict] = []
    for c in raw:
        issues = list(c.get("issues", []))
        start = c.get("_start")
        if start is None or src.text[start:start + len(c["quote"])] != c["quote"]:
            start, qi = D.locate_quote(src.text, c["quote"])
            issues += qi
        made.append({"quote": c["quote"], "fields": c["fields"], "issues": issues, "start": start})
    D.mark_contradictions([{"fields": m["fields"], "issues": m["issues"]} for m in made])  # operates on shared issue lists
    cands = []
    for m in made:
        span_ids = []
        if m["start"] is not None:
            sp = make_span(db, actor, src, m["start"], m["start"] + len(m["quote"]), mode)
            span_ids.append(sp.span_id)
            m["issues"] += D._ref_issues(src.text, m["quote"]) if mode != "DETERMINISTIC" else []
        if src.risk_flags:
            m["issues"].append("SOURCE_HAS_INJECTION_PATTERNS")
        m["issues"] = sorted(set(m["issues"]))
        cand = RegCandidate(tenant_id=actor.tenant_id, source_id=src.source_id, mode=mode, fields=m["fields"], span_ids=span_ids,
                            issues=m["issues"], grounded=bool(span_ids), run_hash=run_hash, created_by=actor.user_id,
                            confidence_category={"REPLAY": "REPLAY_LABELLED", "DETERMINISTIC": "PATTERN_MATCH", "MANUAL": "MANUAL"}[mode])
        db.add(cand)
        cands.append(cand)
    db.flush()
    _ev(db, actor, "CANDIDATES_EXTRACTED", "reg_source", src.source_id, corr=corr, input_hashes=[src.sha256], output_hash=run_hash,
        source_mode=mode, source_spans=[{"span_id": s, "document_id": src.source_id} for c in cands for s in c.span_ids],
        rationale=f"{mode}: {len(cands)} candidates, {sum(1 for c in cands if not c.grounded)} ungrounded; proposals only, nothing activated",
        payload={"mode": mode, "candidates": len(cands)})
    return cands


# ------------------------------------------------------------------ rules
def _markets(scope: dict) -> list[str]:
    out: set[str] = set()

    def walk(n):
        if isinstance(n, dict):
            for k, v in n.items():
                if k == "eq" and isinstance(v, list) and len(v) == 2 and v[0] == "market_type" and isinstance(v[1], str):
                    out.add(v[1])
                elif k == "in" and isinstance(v, list) and v and v[0] == "market_type":
                    out.update(x for x in v[1] if isinstance(x, str))
                else:
                    walk(v)
        elif isinstance(n, list):
            for x in n:
                walk(x)
    walk(scope)
    return sorted(out)


def content_of(r: RegRule) -> dict:
    return {"rule_key": r.rule_key, "version": r.version, "authority_type": r.authority_type, "scope": r.scope,
            "condition": r.condition, "parameters": r.parameters, "exceptions": r.exceptions,
            "effective_from": r.effective_from, "effective_to": r.effective_to, "conflict_group": r.conflict_group,
            "precedence_rank": r.precedence_rank, "additive_stricter": r.additive_stricter, "span_ids": sorted(r.span_ids)}


def compute_hash(r: RegRule) -> str:
    return domain_hash("aether.reg.rule.v1", content_of(r))


def _normalise_params(parameters: dict | None) -> dict:
    p, errs = dsl.normalise_params(parameters)
    return dsl.params_to_json(p) if not errs else {k: v for k, v in (parameters or {}).items()}


def _apply_content(r: RegRule, b: dict) -> None:
    for k in ("title", "authority_type", "scope", "condition", "exceptions", "conflict_group", "precedence_rank",
              "additive_stricter", "candidate_id"):
        if k in b and b[k] is not None or (k in ("conflict_group", "precedence_rank") and k in b):
            setattr(r, k, b[k])
    if "parameters" in b:
        r.parameters = _normalise_params(b["parameters"])
    if "span_ids" in b:
        r.span_ids = list(b["span_ids"])
    if "effective_from" in b:
        r.effective_from = aware(b["effective_from"], "effective_from")
    if "effective_to" in b:
        r.effective_to = aware(b["effective_to"], "effective_to") if b["effective_to"] else None
    r.markets = _markets(r.scope)
    r.compile_report = dsl.compile_rule(r.scope, r.condition, r.parameters)
    r.content_hash = compute_hash(r)


def _sync_tests(db, r: RegRule, vectors: list[dict] | None) -> None:
    if vectors is None:
        return
    for t in db.execute(select(RegRuleTest).where(RegRuleTest.rule_id == r.rule_id)).scalars():
        db.delete(t)
    db.flush()
    for v in vectors:
        db.add(RegRuleTest(tenant_id=r.tenant_id, rule_id=r.rule_id, rule_revision=r.revision, vector=v))


def create_rule(db, actor: Principal, b: dict, corr: str | None = None, version_override: int | None = None) -> RegRule:
    if not (b.get("rationale") or "").strip():
        raise ValidationFailed("rationale is mandatory", field_errors={"rationale": "required"})
    for k in ("rule_key", "scope", "condition", "authority_type", "effective_from"):
        if b.get(k) in (None, ""):
            raise ValidationFailed(f"{k} required", field_errors={k: "required"})
    from app.engines.regulatory.models import RULE_AUTHORITY
    if b["authority_type"] not in RULE_AUTHORITY:
        raise ValidationFailed("authority_type invalid", field_errors={"authority_type": f"one of {RULE_AUTHORITY}"})
    _lock_key(db, actor.tenant_id, b["rule_key"])
    prev = db.execute(select(RegRule).where(RegRule.tenant_id == actor.tenant_id, RegRule.rule_key == b["rule_key"])
                      .order_by(RegRule.version.desc())).scalars().first()
    r = RegRule(tenant_id=actor.tenant_id, rule_key=b["rule_key"], version=version_override or ((prev.version + 1) if prev else 1),
                status="DRAFT", authority_type=b["authority_type"], scope={}, condition={}, parameters={}, span_ids=[],
                effective_from=aware(b["effective_from"], "effective_from"), maker_id=actor.user_id, rationale=b["rationale"].strip(),
                supersedes_rule_id=prev.rule_id if prev else None, content_hash="", provenance="SYNTHETIC",
                additive_stricter=False, exceptions=[], precedence_rank=None, conflict_group=None, effective_to=None,
                markets=[], review_notes=[], compile_report={},
                source_ref=b.get("source_ref"))
    _apply_content(r, {k: v for k, v in b.items() if k not in ("rule_key", "rationale", "effective_from")} | {"effective_from": b["effective_from"]})
    _spans_exist(db, r)
    try:
        with db.begin_nested():
            db.add(r)
            db.flush()
    except IntegrityError as exc:  # two concurrent FIRST creations of one key (no rows existed to lock)
        raise Conflict("another revision of this rule key was created concurrently; reload and retry", code="REVISION_CONFLICT") from exc
    _sync_tests(db, r, b.get("test_vectors"))
    _ev(db, actor, "RULE_DRAFTED", "reg_rule", r.rule_id, r.revision, corr, output_hash=r.content_hash,
        source_spans=replay.span_refs(db, r.span_ids), rationale=r.rationale,
        payload={"rule_key": r.rule_key, "version": r.version, "compile_ok": r.compile_report["ok"], "parents": _parents(db, r)})
    emit(db, event_type="RuleProposed", tenant_id=actor.tenant_id, correlation_id=corr or new_id(), aggregate_id=r.rule_id,
         actor_id=actor.user_id, aggregate_revision=r.revision, payload={"rule_key": r.rule_key, "version": r.version})
    return r


def _spans_exist(db, r: RegRule) -> None:
    for sid in r.span_ids:
        sp = db.get(RegSpan, sid)
        if sp is None or sp.tenant_id != r.tenant_id:
            raise ValidationFailed(f"span {sid} not found", field_errors={"span_ids": "unknown span"})


def update_draft(db, actor: Principal, rule_id: str, changes: dict, expected_revision: int | None, corr=None) -> RegRule:
    r = get_rule(db, actor.tenant_id, rule_id, lock=True)
    if r.status != "DRAFT":
        raise Conflict(f"only DRAFT rules are editable (status {r.status}); approved content is immutable", code="NOT_EDITABLE")
    if r.maker_id != actor.user_id:
        raise Forbidden("only the author may edit a draft", code="NOT_AUTHOR")
    _check_rev(r, expected_revision)
    if not (changes.get("rationale") or "").strip():
        raise ValidationFailed("rationale is mandatory for edits", field_errors={"rationale": "required"})
    r.rationale = changes["rationale"].strip()
    _apply_content(r, {k: v for k, v in changes.items() if k not in ("rationale", "test_vectors")})
    _spans_exist(db, r)
    r.revision += 1
    r.updated_at = utcnow()
    _sync_tests(db, r, changes.get("test_vectors"))
    _ev(db, actor, "RULE_DRAFT_EDITED", "reg_rule", r.rule_id, r.revision, corr, output_hash=r.content_hash, rationale=r.rationale,
        source_spans=replay.span_refs(db, r.span_ids), payload={"compile_ok": r.compile_report["ok"]})
    return r


def revise(db, actor: Principal, rule_id: str, rationale: str, effective_from: datetime | None, corr=None) -> RegRule:
    """New version cloned from an existing one (amendment OR emergency rollback). Never edits history."""
    old = get_rule(db, actor.tenant_id, rule_id, lock=True)
    tests = [t.vector for t in db.execute(select(RegRuleTest).where(RegRuleTest.rule_id == old.rule_id)).scalars()]
    return create_rule(db, actor, {
        "rule_key": old.rule_key, "authority_type": old.authority_type, "scope": old.scope, "condition": old.condition,
        "parameters": old.parameters, "exceptions": old.exceptions, "span_ids": old.span_ids, "conflict_group": old.conflict_group,
        "precedence_rank": old.precedence_rank, "additive_stricter": old.additive_stricter, "title": old.title,
        "effective_from": effective_from or old.effective_from, "effective_to": old.effective_to, "rationale": rationale,
        "test_vectors": tests}, corr)


def _check_basis(db, r: RegRule) -> list[str]:
    """R-A02: every cited span must re-validate against stored bytes/text, and sources must be a legitimate basis."""
    problems = []
    if not r.span_ids:
        return ["NO_SOURCE_SPAN: a rule must cite at least one validated source span"]
    ok_auth = BASIS_OK.get(r.authority_type, set())
    for sid in r.span_ids:
        sp = db.get(RegSpan, sid)
        if sp is None:
            problems.append(f"SPAN_MISSING:{sid}")
            continue
        src = db.get(RegSource, sp.source_id)
        for p in D.validate_span(src, sp):
            problems.append(f"{p}:{sid}")
        if src.authority_type in ("DRAFT_CONSULTATION", "SUPERSEDED"):
            problems.append(f"SOURCE_NOT_BINDING:{src.authority_type}:{sid}")
        elif src.authority_type not in ok_auth:
            problems.append(f"SOURCE_AUTHORITY_MISMATCH:{src.authority_type}!~{r.authority_type}")
        if src.unreadable_pages and sp.page in src.unreadable_pages:
            problems.append(f"MISSING_SOURCE_PAGE:{sp.page}")
    return problems


def submit(db, actor: Principal, rule_id: str, reason: str, expected_revision: int | None, corr=None) -> RegRule:
    r = get_rule(db, actor.tenant_id, rule_id, lock=True)
    if r.status != "DRAFT":
        raise Conflict(f"only DRAFT can be submitted (status {r.status})", code="ILLEGAL_TRANSITION")
    if r.maker_id != actor.user_id:
        raise Forbidden("only the author may submit", code="NOT_AUTHOR")
    _check_rev(r, expected_revision)
    if not reason.strip():
        raise ValidationFailed("reason mandatory", field_errors={"reason": "required"})
    if not r.compile_report.get("ok"):
        raise Blocked("Rule does not compile; review required: " + "; ".join(f"{e['path']}: {e['message']}" for e in r.compile_report.get("errors", [])), code="COMPILE_FAILED")
    problems = _check_basis(db, r)
    if problems:
        raise Blocked("Source basis invalid: " + "; ".join(problems), code="SOURCE_BASIS_INVALID")
    tests = db.execute(select(RegRuleTest).where(RegRuleTest.rule_id == r.rule_id)).scalars().all()
    if not tests:
        raise Blocked("At least one test vector is required before review", code="NO_TEST_VECTORS")
    res = dsl.run_test_vectors(r.scope, r.condition, r.parameters, [t.vector for t in tests])
    for t, x in zip(tests, res):
        t.last_result, t.passed, t.rule_revision = x, x["passed"], r.revision
    if not all(x["passed"] for x in res):
        raise Blocked(f"{sum(1 for x in res if not x['passed'])} test vector(s) failed", code="TESTS_FAILED")
    r.status = "REVIEW_REQUIRED"
    r.updated_at = utcnow()
    r.review_notes = list(r.review_notes) + [{"actor": actor.user_id, "decision": "SUBMIT", "reason": reason.strip(), "at": utcnow().isoformat()}]
    r.revision += 1
    db.add(ApprovalRow(tenant_id=actor.tenant_id, action_type="RULE_APPROVAL", target_type="reg_rule", target_id=r.rule_id,
                       target_revision=r.revision, target_hash=r.content_hash, maker_id=actor.user_id, reason=reason.strip(),
                       expires_at=utcnow() + timedelta(seconds=get_settings().approval_ttl_seconds)))
    db.flush()
    _ev(db, actor, "RULE_SUBMITTED", "reg_rule", r.rule_id, r.revision, corr, output_hash=r.content_hash, rationale=reason,
        source_spans=replay.span_refs(db, r.span_ids), payload={"tests_passed": len(res)})
    return r


def _deny(db, actor, r, code: str, msg: str, corr):
    """Record the denied attempt (separately committed) BEFORE raising, so the rejection is not rolled back."""
    _ev(db, actor, "APPROVAL_DENIED", "reg_rule", r.rule_id, r.revision, corr, rationale=msg,
        payload={"code": code, "attempted_by": actor.user_id, "maker": r.maker_id})
    db.commit()
    raise Forbidden(msg, code=code)


def review(db, actor: Principal, rule_id: str, decision: str, reason: str, expected_revision: int | None, corr=None) -> RegRule:
    r = get_rule(db, actor.tenant_id, rule_id, lock=True)
    if decision not in ("APPROVE", "REJECT"):
        raise ValidationFailed("decision must be APPROVE or REJECT", field_errors={"decision": "invalid"})
    if not reason.strip():
        raise ValidationFailed("reason mandatory", field_errors={"reason": "required"})
    if r.status != "REVIEW_REQUIRED":
        raise Conflict(f"rule is {r.status}, not REVIEW_REQUIRED", code="ILLEGAL_TRANSITION")
    if actor.user_id == r.maker_id:
        _deny(db, actor, r, "SELF_APPROVAL", "Maker cannot approve their own work (independent checker required)", corr)
    if not actor.has("checker", "compliance_officer"):
        _deny(db, actor, r, "CHECKER_ROLE_REQUIRED", "Checker role required", corr)
    _check_rev(r, expected_revision)
    ap = db.execute(select(ApprovalRow).where(ApprovalRow.tenant_id == actor.tenant_id, ApprovalRow.target_id == r.rule_id,
                                              ApprovalRow.decision == "PENDING")).scalars().first()
    now = utcnow()
    if ap is None or ap.target_hash != r.content_hash or ap.target_revision != r.revision:
        raise Conflict("no pending approval bound to this exact revision/hash (stale)", code="STALE_APPROVAL")
    if ap.expires_at <= now:
        ap.decision = "EXPIRED"
        r.status, r.revision = "DRAFT", r.revision + 1
        _ev(db, actor, "APPROVAL_EXPIRED", "reg_rule", r.rule_id, r.revision, corr)
        db.commit()
        raise Conflict("review window expired; rule returned to DRAFT", code="APPROVAL_EXPIRED")
    ap.checker_id, ap.decided_at = actor.user_id, now
    r.review_notes = list(r.review_notes) + [{"actor": actor.user_id, "decision": decision, "reason": reason.strip(), "at": now.isoformat()}]
    if decision == "APPROVE":
        ap.decision, ap.reason = "APPROVED", f"{ap.reason} | checker: {reason.strip()}"
        ap.expires_at = now + timedelta(seconds=get_settings().approval_ttl_seconds)  # window to activate
        r.status, r.approved_hash = "APPROVED", r.content_hash
    else:
        ap.decision = "REJECTED"
        r.status, r.approved_hash = "DRAFT", None  # rejection returns a revisable draft
    r.revision += 1
    r.updated_at = now
    ap.revision += 1
    db.flush()
    _ev(db, actor, "RULE_APPROVED" if decision == "APPROVE" else "RULE_REJECTED", "reg_rule", r.rule_id, r.revision, corr,
        output_hash=r.content_hash, rationale=reason, source_spans=replay.span_refs(db, r.span_ids),
        payload={"approval_id": ap.approval_id, "maker": r.maker_id, "checker": actor.user_id, "parents": _parents(db, r),
                 **({"replay": replay.rule_replay_spec(db, r)} if decision == "APPROVE" else {})})
    if decision == "APPROVE":
        _link_approval(db, actor, corr, r, ap.approval_id)
        emit(db, event_type="RuleApproved", tenant_id=actor.tenant_id, correlation_id=corr or new_id(), aggregate_id=r.rule_id,
             actor_id=actor.user_id, aggregate_revision=r.revision, payload={"rule_key": r.rule_key, "version": r.version})
    return r


def activate(db, actor: Principal, rule_id: str, expected_revision: int | None, reason: str, as_of: datetime | None = None,
             recorded_at: datetime | None = None, corr=None) -> RegRule:
    """Explicit human activation. Future effective_from => SCHEDULED (never ACTIVE before its effective time).
    `as_of` is the scenario clock for that decision; `recorded_at` is for deterministic fixtures only (not exposed on the API)."""
    r = get_rule(db, actor.tenant_id, rule_id, lock=True)
    if not reason.strip():
        raise ValidationFailed("reason mandatory", field_errors={"reason": "required"})
    if r.status != "APPROVED":
        raise Conflict(f"only APPROVED rules can be activated (status {r.status})", code="NOT_APPROVED")
    _check_rev(r, expected_revision)
    now = utcnow()
    ap = db.execute(select(ApprovalRow).where(ApprovalRow.tenant_id == actor.tenant_id, ApprovalRow.target_id == r.rule_id,
                                              ApprovalRow.decision == "APPROVED").order_by(ApprovalRow.decided_at.desc())).scalars().first()
    if ap is None:
        raise Blocked("No recorded approval", code="NO_APPROVAL")
    if ap.expires_at <= now:
        ap.decision = "EXPIRED"
        r.status, r.revision, r.approved_hash = "DRAFT", r.revision + 1, None
        _ev(db, actor, "APPROVAL_EXPIRED", "reg_rule", r.rule_id, r.revision, corr)
        db.commit()
        raise Conflict("approval expired; resubmit for review", code="APPROVAL_EXPIRED")
    # The approval binds the exact content hash; any change after approval invalidates it.
    if r.approved_hash != compute_hash(r) or ap.target_hash != r.approved_hash:
        raise Blocked("Content changed after approval (hash mismatch); approval invalid", code="APPROVAL_HASH_MISMATCH")
    problems = _check_basis(db, r)
    if problems:
        raise Blocked("Source basis invalid at activation: " + "; ".join(problems), code="SOURCE_BASIS_INVALID")
    # a version below one that is already activated must never go live (would leave two ACTIVE versions of one key)
    if db.execute(select(RegRule.version).where(RegRule.tenant_id == actor.tenant_id, RegRule.rule_key == r.rule_key,
                                                RegRule.version > r.version, RegRule.activated_at.is_not(None),
                                                RegRule.status.in_(("SCHEDULED", "ACTIVE", "SUPERSEDED")))).first():
        raise Conflict(f"a later version of {r.rule_key} is already activated; v{r.version} cannot be activated out of order",
                       code="OUT_OF_ORDER_ACTIVATION")
    # no overlapping contradictory revisions: a new version must take effect strictly after the version it supersedes
    prior = db.execute(select(RegRule).where(RegRule.tenant_id == actor.tenant_id, RegRule.rule_key == r.rule_key,
                                             RegRule.version < r.version, RegRule.activated_at.is_not(None),
                                             RegRule.status.in_(("SCHEDULED", "ACTIVE")))
                       .order_by(RegRule.version.desc())).scalars().first()
    if prior and r.effective_from <= prior.effective_from:
        raise Conflict(f"effective_from {r.effective_from.isoformat()} does not follow v{prior.version} "
                       f"({prior.effective_from.isoformat()}): overlapping revision rejected", code="OVERLAPPING_REVISION")
    clock = aware(as_of, "as_of") if as_of else now
    r.status = "SCHEDULED" if r.effective_from > clock else "ACTIVE"
    r.activated_at = aware(recorded_at, "recorded_at") if recorded_at else now
    r.revision += 1
    r.updated_at = now
    if prior:
        prior.superseded_by_rule_id = r.rule_id
        if r.status == "ACTIVE":
            prior.status = "SUPERSEDED"
    db.flush()
    _ev(db, actor, "RULE_ACTIVATED" if r.status == "ACTIVE" else "RULE_SCHEDULED", "reg_rule", r.rule_id, r.revision, corr,
        effective_at=r.effective_from, output_hash=r.content_hash, rationale=reason,
        source_spans=replay.span_refs(db, r.span_ids),
        payload={"rule_key": r.rule_key, "version": r.version, "status": r.status, "approval_id": ap.approval_id,
                 "supersedes": prior.rule_id if prior else None, "parents": _parents(db, r), "replay": replay.rule_replay_spec(db, r)})
    emit(db, event_type="RuleActivated", tenant_id=actor.tenant_id, correlation_id=corr or new_id(), aggregate_id=r.rule_id,
         actor_id=actor.user_id, aggregate_revision=r.revision, effective_at=r.effective_from,
         payload={"rule_key": r.rule_key, "version": r.version, "status": r.status})
    if prior:
        _ev(db, actor, "RULE_SUPERSEDED", "reg_rule", prior.rule_id, prior.revision, corr, payload={"by": r.rule_id, "pending": r.status == "SCHEDULED"})
        emit(db, event_type="RuleSuperseded", tenant_id=actor.tenant_id, correlation_id=corr or new_id(), aggregate_id=prior.rule_id,
             actor_id=actor.user_id, payload={"by_rule_id": r.rule_id})
    return r


def advance_states(db, actor: Principal, now: datetime, corr=None) -> list[str]:
    """Persist clock-driven SCHEDULED->ACTIVE (and predecessor SUPERSEDED). Explicit clock; never implicit."""
    changed = []
    # Row locks: two concurrent callers must not both flip the same SCHEDULED rule (duplicate events / double revision bump).
    for r in db.execute(lock_for_update(select(RegRule).where(RegRule.tenant_id == actor.tenant_id, RegRule.status == "SCHEDULED")
                                        .order_by(RegRule.rule_key, RegRule.version)).execution_options(populate_existing=True)).scalars().all():
        if r.effective_from <= aware(now, "now"):
            r.status = "ACTIVE"
            r.revision += 1
            if r.supersedes_rule_id:
                p = db.get(RegRule, r.supersedes_rule_id)
                if p and p.status == "ACTIVE":
                    p.status = "SUPERSEDED"
            _ev(db, actor, "RULE_BECAME_EFFECTIVE", "reg_rule", r.rule_id, r.revision, corr, effective_at=r.effective_from,
                payload={"rule_key": r.rule_key, "version": r.version})
            changed.append(r.rule_id)
    return changed


def withdraw(db, actor: Principal, rule_id: str, reason: str, expected_revision: int | None, recorded_at: datetime | None = None, corr=None) -> RegRule:
    r = get_rule(db, actor.tenant_id, rule_id, lock=True)
    if not reason.strip():
        raise ValidationFailed("reason mandatory", field_errors={"reason": "required"})
    if r.status not in ("APPROVED", "SCHEDULED", "ACTIVE"):
        raise Conflict(f"cannot withdraw a rule in status {r.status}", code="ILLEGAL_TRANSITION")
    _check_rev(r, expected_revision)
    r.status, r.withdrawn_at = "WITHDRAWN", aware(recorded_at) if recorded_at else utcnow()
    r.revision += 1
    _ev(db, actor, "RULE_WITHDRAWN", "reg_rule", r.rule_id, r.revision, corr, rationale=reason, payload={"rule_key": r.rule_key, "version": r.version})
    return r


# ------------------------------------------------------------------ diff (R04)
def _flatten(x, prefix=""):
    if isinstance(x, dict):
        for k, v in x.items():
            yield from _flatten(v, f"{prefix}.{k}" if prefix else k)
    elif isinstance(x, list):
        for i, v in enumerate(x):
            yield from _flatten(v, f"{prefix}[{i}]")
    else:
        yield prefix, x


def diff(a: RegRule, b: RegRule) -> dict:
    """a = older, b = newer. Added/removed/modified leaves for scope, condition, parameters, plus dates and spans."""
    out = {"added": [], "removed": [], "modified": [], "affected_fields": [], "source_spans": {"from": a.span_ids, "to": b.span_ids}}
    for part in ("scope", "condition", "parameters", "exceptions"):
        fa, fb = dict(_flatten(getattr(a, part), part)), dict(_flatten(getattr(b, part), part))
        for k in sorted(fb.keys() - fa.keys()):
            out["added"].append({"path": k, "value": str(fb[k])})
        for k in sorted(fa.keys() - fb.keys()):
            out["removed"].append({"path": k, "value": str(fa[k])})
        for k in sorted(fa.keys() & fb.keys()):
            if fa[k] != fb[k]:
                out["modified"].append({"path": k, "from": str(fa[k]), "to": str(fb[k])})
    for k in ("effective_from", "effective_to", "authority_type", "conflict_group", "precedence_rank", "additive_stricter"):
        va, vb = getattr(a, k), getattr(b, k)
        if va != vb:
            out["modified"].append({"path": k, "from": None if va is None else str(va), "to": None if vb is None else str(vb)})
    deps = set(a.compile_report.get("dependencies", [])) | set(b.compile_report.get("dependencies", []))
    out["affected_fields"] = sorted(deps)
    out["identical"] = not (out["added"] or out["removed"] or out["modified"])
    return out

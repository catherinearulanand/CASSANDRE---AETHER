"""A2 COVENANT services (DB-backed). Public cross-engine contract: contracts.resolve_terms, contracts.netting_membership
(+ additive: contracts.terms_by_hash). Every consequential write appends an evidence event IN THE SAME TRANSACTION.

Terminology: a *field* is one reviewed extraction; a *version* is an immutable frozen bundle of approved,
effective-dated entries; resolution picks the version known at `known_at` and evaluates entries at `as_of`.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.auth.security import Principal, require_distinct_checker
from app.core import services
from app.core.canonical import canonical_bytes, content_hash, sha256_hex
from app.core.config import get_settings
from app.core.errors import (Blocked, Conflict, Forbidden, NotFound, NotImplementedCapability, ValidationFailed)
from app.core.events import emit
from app.core.money import SUPPORTED_CURRENCIES
from app.db.base import lock_for_update, new_id, utcnow
from app.db.models_core import ApprovalRow
from app.engines.contracts import extract as X
from app.engines.contracts import terms as T
from app.engines.contracts.models import (ConDocument, ConEligibility, ConFactSnapshot, ConField, ConNettingMembership,
                                          ConPackage, ConResolution, ConVersion)

MAX_DOC_CHARS = 500_000
MIN_REASON = 8
ENGINE_VERSION = "covenant/2.0"
METHOD_VERSION = "terms-resolution/1"  # bump when resolution/bundle-hash semantics change; old events then replay as VERSION_MISMATCH


def _insert_ignore(db, model, values: dict) -> None:
    """Insert-if-absent that is safe under concurrency (PostgreSQL raised a unique violation for the second concurrent
    resolver of identical content; SQLite's serialised writer had masked it). Content-addressed rows only."""
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as ins
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as ins
    else:  # pragma: no cover - other dialects: savepoint fallback
        try:
            with db.begin_nested():
                db.add(model(**values))
        except IntegrityError:
            pass
        return
    db.execute(ins(model).values(**values).on_conflict_do_nothing())


def iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def _replay_spec(business_hash: str, inputs: dict) -> dict:
    """payload.replay contract (docs/agent-handoffs/CONTRACTS.md v1.1). The business hash excludes run IDs, timestamps
    and audit sequence numbers by construction (it is the content hash of bundle + terms + facts)."""
    return {"replayer": "replay.contracts", "engine_version": ENGINE_VERSION, "method_version": METHOD_VERSION,
            "business_hash": business_hash, "inputs": inputs}


def _parents(*pairs):
    seen, out = set(), []
    for t, i in pairs:
        if (t, i) not in seen:
            seen.add((t, i))
            out.append({"entity_type": t, "entity_id": i})
    return out


def _ev(db, p: Principal, corr: str, action: str, etype: str, eid: str, *, rationale=None, payload=None,
        input_hashes=None, output_hash=None, effective_at=None, mode="RULES", revision=1, spans=None,
        parents=None, replay=None):
    payload = dict(payload or {})
    if parents:
        payload["parents"] = parents
    if replay:
        payload["replay"] = replay
    return services.call("evidence.append", db, p.tenant_id, engine="contracts", action=action, actor_id=p.user_id,
                         actor_type="USER", actor_role=",".join(p.roles), entity_type=etype, entity_id=eid,
                         entity_revision=revision, correlation_id=corr, effective_at=effective_at, source_mode=mode,
                         input_hashes=input_hashes or [], source_spans=spans or [], output_hash=output_hash,
                         rationale=rationale, payload=payload)


def _scope(p: Principal) -> None:
    if p.entitlements and "OTC" not in p.entitlements:
        raise Forbidden("OTC market entitlement required")


def _need_role(p: Principal, *roles: str) -> None:
    if not p.has(*roles):
        raise Forbidden(f"Requires one of roles: {', '.join(roles)}")


def _reason(reason: str | None) -> str:
    if not reason or len(reason.strip()) < MIN_REASON:
        raise ValidationFailed("A reason of at least 8 characters is mandatory", field_errors={"reason": "required"})
    return reason.strip()


def get_package(db, tenant_id: str, package_id: str, lock: bool = False) -> ConPackage:
    q = select(ConPackage).where(ConPackage.tenant_id == tenant_id, ConPackage.package_id == package_id)
    if lock:
        q = lock_for_update(q).execution_options(populate_existing=True)
    pkg = db.execute(q).scalar_one_or_none()
    if pkg is None:
        raise NotFound("Agreement package not found")
    return pkg


def _docs(db, pkg) -> list[ConDocument]:
    return list(db.execute(select(ConDocument).where(ConDocument.package_id == pkg.package_id)
                           .order_by(ConDocument.precedence_rank, ConDocument.effective_from, ConDocument.doc_id)).scalars())


def _fields(db, pkg) -> list[ConField]:
    return list(db.execute(select(ConField).where(ConField.package_id == pkg.package_id)
                           .order_by(ConField.created_at, ConField.field_key, ConField.field_id)).scalars())


def _versions(db, pkg) -> list[ConVersion]:
    return list(db.execute(select(ConVersion).where(ConVersion.package_id == pkg.package_id)
                           .order_by(ConVersion.version_no)).scalars())


def _touch(db, pkg: ConPackage) -> None:
    """Any content change bumps the package revision and expires pending freeze approvals (stale-approval guard)."""
    pkg.revision += 1
    pkg.updated_at = utcnow()
    for a in db.execute(select(ApprovalRow).where(ApprovalRow.tenant_id == pkg.tenant_id, ApprovalRow.target_id == pkg.package_id,
                                                  ApprovalRow.action_type == "TERMS_FREEZE",
                                                  ApprovalRow.decision == "PENDING")).scalars():
        a.decision = "EXPIRED"
        a.decided_at = utcnow()


def _refresh_status(db, pkg: ConPackage) -> None:
    has_ver = bool(_versions(db, pkg))
    pending = any(f.frozen_version_id is None for f in _fields(db, pkg)) or any(d.extracted_mode is None for d in _docs(db, pkg))
    if has_ver:
        pkg.status = "AMENDMENT_IN_REVIEW" if pending else "FROZEN"
    else:
        pkg.status = "IN_REVIEW" if any(d.extracted_mode for d in _docs(db, pkg)) else "DRAFT"


# ----------------------------------------------------------------------------------------- packages / docs
def _validate_doc(i: int, d: dict, pkg_names: tuple[str, str], force_kind: str | None = None) -> dict:
    fe: dict[str, str] = {}
    kind = force_kind or d.get("kind")
    text, ref = d.get("text"), (d.get("doc_ref") or "").strip()
    if kind not in T.DOC_KINDS:
        fe[f"documents.{i}.kind"] = f"must be one of {sorted(T.DOC_KINDS)}"
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_DOC_CHARS or "\x00" in text:
        fe[f"documents.{i}.text"] = "non-empty UTF-8 text up to 500,000 characters without NUL is required"
    if not ref:
        fe[f"documents.{i}.doc_ref"] = "required"
    elif len(ref) > 120:
        fe[f"documents.{i}.doc_ref"] = "at most 120 characters"
    if isinstance(d.get("title"), str) and len(d["title"]) > 300:
        fe[f"documents.{i}.title"] = "at most 300 characters"
    try:
        eff = T.parse_dt(d["effective_from"])
    except Exception:  # noqa: BLE001
        eff = None
        fe[f"documents.{i}.effective_from"] = "ISO-8601 datetime required"
    ma = d.get("missing_annexes") or []
    if not isinstance(ma, list) or any(not isinstance(x, str) for x in ma):
        fe[f"documents.{i}.missing_annexes"] = "list of strings"
    if not fe:
        h = X.header_info(text)
        if h["party_a"] is None:
            fe[f"documents.{i}.text"] = "DOC_PARTY_HEADER_MISSING: 'Party A: <name>. Party B: <name>.' is required"
        elif (h["party_a"], h["party_b"]) != pkg_names:
            fe[f"documents.{i}.text"] = (f"DOC_PARTY_MISMATCH: document says Party A={h['party_a']!r}, Party B={h['party_b']!r} "
                                         f"but the package is A={pkg_names[0]!r}, B={pkg_names[1]!r} (swapped or wrong parties)")
        if h["effective_date"] is None or h["effective_date"] != eff.strftime("%Y-%m-%d"):
            fe[f"documents.{i}.effective_from"] = (f"DOC_DATE_MISMATCH: document header date {h['effective_date']!r} "
                                                   f"differs from effective_from {eff.date()}")
    if fe:
        raise ValidationFailed("Document validation failed", field_errors=fe)
    return {"kind": kind, "title": (d.get("title") or ref)[:300], "doc_ref": ref, "text": text, "effective_from": eff,
            "missing_annexes": list(ma)}


def _add_doc(db, p, pkg: ConPackage, v: dict) -> ConDocument:
    sha = sha256_hex(v["text"].encode("utf-8"))
    if db.execute(select(ConDocument).where(ConDocument.package_id == pkg.package_id, ConDocument.sha256 == sha)).first():
        raise Conflict("This exact document is already in the package", code="DUPLICATE_DOCUMENT")
    doc = ConDocument(tenant_id=p.tenant_id, package_id=pkg.package_id, kind=v["kind"], title=v["title"], doc_ref=v["doc_ref"],
                      sha256=sha, text=v["text"], effective_from=v["effective_from"], precedence_rank=T.PRECEDENCE_RANK[v["kind"]],
                      missing_annexes=v["missing_annexes"], created_by=p.user_id, source_ref=v["doc_ref"])
    db.add(doc)
    db.flush()
    return doc


def create_package(db, p: Principal, corr: str, body: dict) -> dict:
    _scope(p)
    _need_role(p, "maker", "tenant_admin")
    fe = {}
    for k in ("master_id", "csa_id", "netting_set_id", "party_a_id", "party_b_id", "party_a_name", "party_b_name", "governing_law"):
        if not isinstance(body.get(k), str) or not body[k].strip():
            fe[k] = "required"
    if body.get("base_currency") not in SUPPORTED_CURRENCIES:
        fe["base_currency"] = "unsupported or missing ISO currency"
    docs = body.get("documents") or []
    if not docs or len(docs) > 20:
        fe["documents"] = "1..20 documents required"
    if body.get("party_a_id") and body.get("party_a_id") == body.get("party_b_id"):
        fe["party_b_id"] = "parties must differ"
    for k, lim in (("master_id", 80), ("csa_id", 80), ("netting_set_id", 80), ("party_a_id", 80), ("party_b_id", 80),
                   ("party_a_name", 200), ("party_b_name", 200), ("governing_law", 200)):
        if isinstance(body.get(k), str) and len(body[k].strip()) > lim:
            fe[k] = f"at most {lim} characters"
    if fe:
        raise ValidationFailed("Package validation failed", field_errors=fe)
    existing = db.execute(select(ConPackage).where(ConPackage.tenant_id == p.tenant_id,
                                                   ConPackage.netting_set_id == body["netting_set_id"])).scalar_one_or_none()
    if existing is not None:
        code = "CROSS_MASTER_NETTING_SET" if existing.master_id != body["master_id"] else "NETTING_SET_EXISTS"
        raise Conflict(f"Netting set {body['netting_set_id']} is already registered under master {existing.master_id}; "
                       "a netting set belongs to exactly one master (no cross-master netting)", code=code)
    names = (body["party_a_name"], body["party_b_name"])
    vdocs = [_validate_doc(i, d, names) for i, d in enumerate(docs)]
    if len({v["doc_ref"] + sha256_hex(v["text"].encode()) for v in vdocs}) != len(vdocs):
        raise ValidationFailed("Duplicate documents", field_errors={"documents": "duplicate document"})
    pkg = ConPackage(tenant_id=p.tenant_id, master_id=body["master_id"], csa_id=body["csa_id"], netting_set_id=body["netting_set_id"],
                     party_a_id=body["party_a_id"], party_b_id=body["party_b_id"], party_a_name=names[0], party_b_name=names[1],
                     base_currency=body["base_currency"], governing_law=body["governing_law"], created_by=p.user_id,
                     source_ref=body["csa_id"])
    try:
        with db.begin_nested():  # SAVEPOINT: a concurrent creator of the same netting set loses cleanly (no raw 500)
            db.add(pkg)
            db.flush()
    except IntegrityError:
        other = db.execute(select(ConPackage).where(ConPackage.tenant_id == p.tenant_id,
                                                    ConPackage.netting_set_id == body["netting_set_id"])).scalar_one_or_none()
        code = "CROSS_MASTER_NETTING_SET" if other is not None and other.master_id != body["master_id"] else "NETTING_SET_EXISTS"
        raise Conflict(f"Netting set {body['netting_set_id']} was registered concurrently; "
                       "a netting set belongs to exactly one master (no cross-master netting)", code=code) from None
    shas, doc_ids = [], []
    for v in vdocs:
        d = _add_doc(db, p, pkg, v)
        shas.append(d.sha256)
        doc_ids.append(d.doc_id)
    _ev(db, p, corr, "CONTRACT_PACKAGE_CREATED", "contract_package", pkg.package_id,
        payload={"master_id": pkg.master_id, "csa_id": pkg.csa_id, "netting_set_id": pkg.netting_set_id,
                 "documents": [{"doc_ref": v["doc_ref"], "kind": v["kind"]} for v in vdocs]},
        input_hashes=shas, mode="MANUAL", rationale="Agreement package registered (document metadata only; not an enforceability opinion)",
        parents=_parents(*(("contract_document", i) for i in doc_ids)))
    return package_detail(db, p.tenant_id, pkg.package_id)


def add_document(db, p: Principal, corr: str, package_id: str, body: dict, amendment: bool) -> dict:
    _scope(p)
    _need_role(p, "maker", "tenant_admin")
    pkg = get_package(db, p.tenant_id, package_id, lock=True)
    has_ver = bool(_versions(db, pkg))
    if amendment and not has_ver:
        raise Conflict("Amendments apply to frozen terms; freeze the base terms first", code="NO_FROZEN_TERMS_TO_AMEND")
    if not amendment and has_ver:
        raise Conflict("Frozen terms are read-only; additional documents must be filed as amendments", code="PACKAGE_FROZEN")
    v = _validate_doc(0, body, (pkg.party_a_name, pkg.party_b_name), force_kind="AMENDMENT" if amendment else None)
    doc = _add_doc(db, p, pkg, v)
    _touch(db, pkg)
    _refresh_status(db, pkg)
    _ev(db, p, corr, "AMENDMENT_ADDED" if amendment else "CONTRACT_DOCUMENT_ADDED", "contract_document", doc.doc_id,
        payload={"package_id": pkg.package_id, "doc_ref": doc.doc_ref, "kind": doc.kind, "effective_from": iso(doc.effective_from)},
        input_hashes=[doc.sha256], mode="MANUAL", revision=pkg.revision)
    return package_detail(db, p.tenant_id, pkg.package_id)


# ----------------------------------------------------------------------------------------- extraction
def _rebuild_eligibility(db, pkg: ConPackage, f: ConField) -> None:
    for r in db.execute(select(ConEligibility).where(ConEligibility.field_id == f.field_id)).scalars():
        db.delete(r)
    # Only VALIDATED schedules are materialised (an UNRESOLVED field's value failed validation and may not even fit the columns).
    if f.field_key == "eligible_schedule" and isinstance(f.value, list) and f.status in ("PROPOSED", "APPROVED"):
        for r in f.value:
            db.add(ConEligibility(tenant_id=pkg.tenant_id, package_id=pkg.package_id, field_id=f.field_id, field_status=f.status,
                                  **{k: r.get(k) for k in ("asset_class", "currency", "issuer_class", "valuation_pct", "haircut_pct",
                                                           "fx_haircut_pct", "concentration_cap_pct", "cap_denominator",
                                                           "min_rating", "max_maturity_years")}))


def extract(db, p: Principal, corr: str, package_id: str, mode: str = "RULES", doc_id: str | None = None) -> dict:
    _scope(p)
    _need_role(p, "maker")
    if mode == "LIVE_MODEL":
        raise NotImplementedCapability("LIVE_MODEL extraction is not implemented in this build (hosted provider disabled)")
    if mode not in ("RULES", "REPLAY"):
        raise ValidationFailed("mode must be RULES or REPLAY", field_errors={"mode": "invalid"})
    pkg = get_package(db, p.tenant_id, package_id, lock=True)
    docs = [d for d in _docs(db, pkg) if d.extracted_mode is None and (doc_id is None or d.doc_id == doc_id)]
    if not docs:
        raise Conflict("No un-extracted document to process (documents are extracted once; originals are immutable)",
                       code="NOTHING_TO_EXTRACT")
    created, unresolved, warnings = 0, 0, []
    for doc in docs:
        try:
            props = (X.load_replay(doc.doc_ref, doc.sha256) if mode == "REPLAY" else
                     X.extract_rules(doc.text, {"id": pkg.party_a_id, "name": pkg.party_a_name},
                                     {"id": pkg.party_b_id, "name": pkg.party_b_name}, pkg.base_currency))
        except X.ExtractionError as e:
            raise ValidationFailed(str(e), code=e.code) from e
        if not props:
            warnings.append(f"{doc.doc_ref}: no recognised clauses (nothing proposed; required fields will block freeze)")
        for pr in props:
            norm, issues = X.check_proposal(pr, doc.text, pkg.party_a_name, pkg.party_b_name, pkg.base_currency, pkg.netting_set_id)
            f = ConField(tenant_id=p.tenant_id, package_id=pkg.package_id, doc_id=doc.doc_id, field_key=pr["field_key"],
                         direction=pr.get("direction"), value=norm.get("value"), original_value=pr.get("value"),
                         quote=pr.get("quote") or "", start_char=int(pr.get("start") or 0), end_char=int(pr.get("end") or 0),
                         condition=pr.get("condition"), effective_from=doc.effective_from, rank=doc.precedence_rank,
                         extraction_mode=mode, status="UNRESOLVED" if issues else "PROPOSED", issues=issues,
                         maker_id=p.user_id, source_ref=doc.doc_ref)
            db.add(f)
            db.flush()
            _rebuild_eligibility(db, pkg, f)
            created += 1
            unresolved += bool(issues)
        doc.extracted_mode = mode
        _ev(db, p, corr, "CONTRACT_EXTRACTED", "contract_document", doc.doc_id, mode=mode, revision=pkg.revision,
            payload={"package_id": pkg.package_id, "doc_ref": doc.doc_ref, "mode": mode, "fields": len(props)},
            input_hashes=[doc.sha256], output_hash=content_hash([{"k": x["field_key"], "v": x.get("value")} for x in props]),
            rationale=f"{mode} extraction proposals; none are approved. Model confidence is not reported by design.")
    _touch(db, pkg)
    _refresh_status(db, pkg)
    return {"package_id": pkg.package_id, "mode": mode, "fields_created": created, "unresolved": unresolved, "warnings": warnings}


# ----------------------------------------------------------------------------------------- review
def field_out(f: ConField, doc: ConDocument | None = None) -> dict:
    return {"field_id": f.field_id, "doc_id": f.doc_id, "doc_ref": doc.doc_ref if doc else None, "field_key": f.field_key,
            "direction": f.direction, "value": f.value, "original_value": f.original_value, "quote": f.quote,
            "start_char": f.start_char, "end_char": f.end_char, "condition": f.condition,
            "effective_from": iso(f.effective_from), "rank": f.rank, "extraction_mode": f.extraction_mode,
            "status": f.status, "issues": f.issues, "maker_id": f.maker_id, "reviewer_id": f.reviewer_id,
            "review_reason": f.review_reason, "revision": f.revision, "frozen": f.frozen_version_id is not None,
            "material": T.FIELD_CATALOG.get(f.field_key, {}).get("material", False)}


def review_field(db, p: Principal, corr: str, package_id: str, field_id: str, action: str, reason: str | None,
                 expected_revision: int, corrected_value: Any = None, direction: str | None = None) -> dict:
    _scope(p)
    reason = _reason(reason)
    pkg = get_package(db, p.tenant_id, package_id, lock=True)
    f = db.execute(select(ConField).where(ConField.field_id == field_id, ConField.package_id == pkg.package_id,
                                          ConField.tenant_id == p.tenant_id)).scalar_one_or_none()
    if f is None:
        raise NotFound("Field not found")
    if f.frozen_version_id is not None:
        raise Conflict("This field belongs to frozen terms and is read-only; file an amendment", code="FIELD_FROZEN")
    if f.revision != expected_revision:
        raise Conflict(f"Field changed (revision {f.revision}, you sent {expected_revision}); reload", code="REVISION_CONFLICT")
    doc = db.get(ConDocument, f.doc_id)
    before = {"status": f.status, "value": f.value}
    if action == "CORRECT":
        _need_role(p, "maker")
        norm, issues = X.check_proposal({"field_key": f.field_key, "direction": direction or f.direction, "value": corrected_value,
                                         "quote": f.quote, "start": f.start_char, "end": f.end_char, "condition": f.condition,
                                         "unit": None},
                                        doc.text, pkg.party_a_name, pkg.party_b_name, pkg.base_currency, None)
        if issues:
            raise ValidationFailed("Corrected value does not validate", field_errors={f"issue{i + 1}": s for i, s in enumerate(issues)})
        f.value, f.direction, f.status = norm["value"], norm.get("direction"), "PROPOSED"
        f.issues, f.maker_id, f.reviewer_id, f.reviewed_at = [], p.user_id, None, None
    elif action in ("APPROVE", "REJECT", "MARK_UNRESOLVED"):
        _need_role(p, "checker", "compliance_officer") if action != "MARK_UNRESOLVED" else _need_role(p, "maker", "checker", "compliance_officer")
        if action == "APPROVE":
            require_distinct_checker(f.maker_id, p)  # maker never approves their own material data
            if f.status != "PROPOSED":
                raise Blocked(f"Field is {f.status}; only a validated PROPOSED field can be approved"
                              + (": " + "; ".join(f.issues) if f.issues else ""))
            norm, issues = X.check_proposal({"field_key": f.field_key, "direction": f.direction, "value": f.value, "quote": f.quote,
                                             "start": f.start_char, "end": f.end_char, "condition": f.condition, "unit": None},
                                            doc.text, pkg.party_a_name, pkg.party_b_name, pkg.base_currency, None)
            if issues:
                raise Blocked("Re-validation failed at approval: " + "; ".join(issues))
            f.status = "APPROVED"
        elif action == "REJECT":
            require_distinct_checker(f.maker_id, p)
            f.status = "REJECTED"
        else:
            f.status, f.issues = "UNRESOLVED", list(f.issues or []) + [f"MARKED_UNRESOLVED: {reason}"]
        f.reviewer_id, f.reviewed_at = p.user_id, utcnow()
    else:
        raise ValidationFailed("action must be CORRECT, APPROVE, REJECT or MARK_UNRESOLVED", field_errors={"action": "invalid"})
    f.review_reason, f.revision = reason, f.revision + 1
    db.flush()
    _rebuild_eligibility(db, pkg, f)
    _touch(db, pkg)
    _ev(db, p, corr, "CONTRACT_FIELD_REVIEWED", "contract_field", f.field_id, rationale=reason, revision=f.revision,
        mode="MANUAL", input_hashes=[doc.sha256], output_hash=content_hash({"v": f.value, "s": f.status}),
        spans=[{"doc_sha256": doc.sha256, "start": f.start_char, "end": f.end_char, "quote": f.quote}],
        parents=_parents(("contract_document", doc.doc_id)),
        payload={"package_id": pkg.package_id, "field_key": f.field_key, "action": action, "status_before": before["status"],
                 "status_after": f.status, "value_hash_before": content_hash({"v": before["value"]}),
                 "value_hash_after": content_hash({"v": f.value})})
    return field_out(f, doc)


# ----------------------------------------------------------------------------------------- freeze
def _bundle_hash(package_static: dict, entries: list) -> str:
    """Single definition of the frozen-bundle hash, used at freeze time AND by replay (so they cannot drift apart)."""
    hash_entries = sorted(({k: e[k] for k in ("key", "direction", "value", "condition", "effective_from", "rank", "doc_sha256", "start", "end")}
                           for e in entries), key=lambda e: canonical_bytes(e))
    return content_hash({"package": package_static, "entries": hash_entries})


def _candidate(db, pkg: ConPackage) -> tuple[list[dict], list[str], str, list[ConField]]:
    fields, docs = _fields(db, pkg), {d.doc_id: d for d in _docs(db, pkg)}
    entries = []
    for f in fields:
        if f.status != "APPROVED":
            continue
        d = docs[f.doc_id]
        entries.append({"key": f.field_key, "direction": f.direction, "value": f.value, "condition": f.condition,
                        "effective_from": iso(f.effective_from), "rank": f.rank, "doc_sha256": d.sha256, "doc_ref": d.doc_ref,
                        "doc_id": d.doc_id, "field_id": f.field_id, "start": f.start_char, "end": f.end_char, "quote": f.quote})
    states = [{"field_key": f.field_key, "status": f.status} for f in fields]
    blocking = T.freeze_blocking(entries, states, [{"doc_ref": d.doc_ref, "missing_annexes": d.missing_annexes} for d in docs.values()])
    if not fields:
        blocking = sorted(set(blocking) | {"NO_FIELDS_EXTRACTED"})
    net = [e for e in entries if e["key"] == "netting_authorization" and not e["condition"]]
    if net and not any(e["value"] is True for e in net):
        blocking.append("NETTING_NOT_AUTHORISED")
    bundle = _bundle_hash({"master_id": pkg.master_id, "csa_id": pkg.csa_id, "netting_set_id": pkg.netting_set_id,
                           "party_a_id": pkg.party_a_id, "party_b_id": pkg.party_b_id, "base_currency": pkg.base_currency}, entries)
    return entries, sorted(set(blocking)), bundle, fields


def readiness(db, tenant_id: str, package_id: str) -> dict:
    pkg = get_package(db, tenant_id, package_id)
    entries, blocking, bundle, _ = _candidate(db, pkg)
    vers = _versions(db, pkg)
    # The currently VALID pending approval (same hash + revision, unexpired): lets an independent checker, in a different
    # session, find what the maker proposed. A stale/expired one is never shown as actionable.
    pend = db.execute(select(ApprovalRow).where(ApprovalRow.tenant_id == tenant_id, ApprovalRow.target_id == pkg.package_id,
                                                ApprovalRow.action_type == "TERMS_FREEZE", ApprovalRow.decision == "PENDING",
                                                ApprovalRow.target_hash == bundle, ApprovalRow.target_revision == pkg.revision,
                                                ApprovalRow.expires_at > utcnow()).order_by(ApprovalRow.created_at.desc())).scalars().first()
    return {"ready_to_freeze": not blocking and not (vers and vers[-1].bundle_hash == bundle), "blocking_reasons": blocking,
            "candidate_bundle_hash": bundle, "required_fields": T.REQUIRED_KEYS,
            "unchanged_from_latest_frozen": bool(vers and vers[-1].bundle_hash == bundle),
            "pending_approval": approval_out(pend) if pend is not None else None}


def propose_freeze(db, p: Principal, corr: str, package_id: str, reason: str | None) -> dict:
    _scope(p)
    _need_role(p, "maker")
    reason = _reason(reason)
    pkg = get_package(db, p.tenant_id, package_id, lock=True)
    entries, blocking, bundle, _ = _candidate(db, pkg)
    if blocking:
        raise Blocked("Terms cannot be frozen: " + "; ".join(blocking), field_errors={f"blocking_{i + 1}": b for i, b in enumerate(blocking)})
    vers = _versions(db, pkg)
    if vers and vers[-1].bundle_hash == bundle:
        raise Conflict("Nothing to freeze: approved content equals the latest frozen version", code="NOTHING_TO_FREEZE")
    now = utcnow()
    ap = db.execute(select(ApprovalRow).where(ApprovalRow.tenant_id == p.tenant_id, ApprovalRow.target_id == pkg.package_id,
                                              ApprovalRow.action_type == "TERMS_FREEZE", ApprovalRow.decision == "PENDING",
                                              ApprovalRow.target_hash == bundle, ApprovalRow.target_revision == pkg.revision,
                                              ApprovalRow.expires_at > now)).scalars().first()
    if ap is None:
        ap = ApprovalRow(tenant_id=p.tenant_id, action_type="TERMS_FREEZE", target_type="contract_terms", target_id=pkg.package_id,
                         target_revision=pkg.revision, target_hash=bundle, maker_id=p.user_id, reason=reason,
                         expires_at=now + timedelta(seconds=get_settings().approval_ttl_seconds))
        db.add(ap)
        db.flush()
        _ev(db, p, corr, "TERMS_FREEZE_PROPOSED", "contract_terms", pkg.package_id, rationale=reason, revision=pkg.revision,
            mode="MANUAL", output_hash=bundle, parents=_parents(*(("contract_field", e["field_id"]) for e in entries)),
            payload={"approval_id": ap.approval_id, "bundle_hash": bundle, "approved_entries": len(entries)})
    return approval_out(ap)


def approval_out(a: ApprovalRow) -> dict:
    return {"approval_id": a.approval_id, "decision": a.decision, "target_hash": a.target_hash, "target_revision": a.target_revision,
            "maker_id": a.maker_id, "checker_id": a.checker_id, "reason": a.reason, "expires_at": iso(a.expires_at)}


def freeze(db, p: Principal, corr: str, package_id: str, approval_id: str, reason: str | None) -> dict:
    _scope(p)
    _need_role(p, "checker", "compliance_officer")
    reason = _reason(reason)
    pkg = get_package(db, p.tenant_id, package_id, lock=True)
    ap = db.execute(select(ApprovalRow).where(ApprovalRow.approval_id == approval_id, ApprovalRow.tenant_id == p.tenant_id,
                                              ApprovalRow.target_id == pkg.package_id,
                                              ApprovalRow.action_type == "TERMS_FREEZE")).scalar_one_or_none()
    if ap is None:
        raise NotFound("Approval not found")
    if ap.decision != "PENDING":
        raise Conflict(f"Approval is {ap.decision}; it cannot be used again", code="APPROVAL_NOT_PENDING")
    if utcnow() > ap.expires_at or ap.target_revision != pkg.revision:
        raise Conflict("Approval is stale: package content changed or approval expired; propose again", code="STALE_APPROVAL")
    require_distinct_checker(ap.maker_id, p)
    entries, blocking, bundle, fields = _candidate(db, pkg)
    if blocking:
        raise Blocked("Terms cannot be frozen: " + "; ".join(blocking))
    if bundle != ap.target_hash:
        raise Conflict("Approval hash does not match current approved content", code="STALE_APPROVAL")
    vers = _versions(db, pkg)
    docs = _docs(db, pkg)
    prev = vers[-1] if vers else None
    facts = sorted({f for e in entries if e["condition"] for f in T.predicate_facts(e["condition"])})
    ver = ConVersion(version_id=new_id(), tenant_id=p.tenant_id, package_id=pkg.package_id, master_id=pkg.master_id, netting_set_id=pkg.netting_set_id,
                     version_no=(prev.version_no + 1) if prev else 1, bundle_hash=bundle,
                     entries=[{k: e[k] for k in ("key", "direction", "value", "condition", "effective_from", "rank", "doc_sha256",
                                                 "doc_ref", "start", "end", "quote", "field_id")} for e in entries],
                     pkg_static={"party_a_id": pkg.party_a_id, "party_b_id": pkg.party_b_id, "base_currency": pkg.base_currency},
                     source_graph={"documents": [{"doc_id": d.doc_id, "doc_ref": d.doc_ref, "kind": d.kind, "sha256": d.sha256,
                                                  "rank": d.precedence_rank, "effective_from": iso(d.effective_from)} for d in docs]},
                     required_facts=facts, approval_id=ap.approval_id, maker_id=ap.maker_id, checker_id=p.user_id,
                     supersedes_version_id=prev.version_id if prev else None, source_ref=pkg.csa_id)
    db.add(ver)
    if prev:
        prev.status = "SUPERSEDED"
    for f in fields:
        if f.status == "APPROVED" and f.frozen_version_id is None:
            f.frozen_version_id = ver.version_id
    ap.decision, ap.checker_id, ap.decided_at = "APPROVED", p.user_id, utcnow()
    pkg.status = "FROZEN"
    db.flush()
    spans = [{"doc_sha256": e["doc_sha256"], "start": e["start"], "end": e["end"], "quote": e["quote"], "key": e["key"]} for e in entries]
    field_parents = _parents(*(("contract_field", e["field_id"]) for e in entries))
    frozen_replay = _replay_spec(bundle, {"kind": "FROZEN_BUNDLE", "version_id": ver.version_id})
    # The approval is its own lineage node (APPROVAL stage): fields -> approval -> frozen terms.
    _ev(db, p, corr, "TERMS_APPROVED", "approval", ap.approval_id, rationale=reason, revision=ver.version_no, mode="MANUAL",
        output_hash=bundle, input_hashes=[d.sha256 for d in docs],
        parents=field_parents,
        payload={"approval_id": ap.approval_id, "maker_id": ap.maker_id, "checker_id": p.user_id, "bundle_hash": bundle})
    _ev(db, p, corr, "TERMS_FROZEN", "contract_terms", pkg.package_id, rationale=reason, revision=ver.version_no, mode="MANUAL",
        output_hash=bundle, input_hashes=[d.sha256 for d in docs], spans=spans,
        parents=field_parents + _parents(("approval", ap.approval_id)),
        replay=frozen_replay,
        payload={"version_id": ver.version_id, "version_no": ver.version_no, "netting_set_id": pkg.netting_set_id,
                 "supersedes": ver.supersedes_version_id, "bundle_hash": bundle})
    for et in ("TermsApproved", "TermsFrozen"):
        emit(db, event_type=et, tenant_id=p.tenant_id, correlation_id=corr, aggregate_id=pkg.package_id, actor_id=p.user_id,
             aggregate_revision=ver.version_no, payload={"version_id": ver.version_id, "bundle_hash": bundle,
                                                         "netting_set_id": pkg.netting_set_id})
    return version_out(ver, entries=True)


def version_out(v: ConVersion, entries: bool = False) -> dict:
    o = {"version_id": v.version_id, "package_id": v.package_id, "version_no": v.version_no, "bundle_hash": v.bundle_hash,
         "status": v.status, "frozen_at": v.frozen_at.isoformat(), "maker_id": v.maker_id, "checker_id": v.checker_id,
         "approval_id": v.approval_id, "supersedes_version_id": v.supersedes_version_id, "required_facts": v.required_facts,
         "netting_set_id": v.netting_set_id, "master_id": v.master_id}
    if entries:
        o["entries"] = v.entries
        o["source_graph"] = v.source_graph
    return o


# ----------------------------------------------------------------------------------------- resolution (public contract)
def _snapshot_facts(db, tenant_id: str, fact_snapshot_id: str | None):
    if not fact_snapshot_id:
        return None, None
    s = db.execute(select(ConFactSnapshot).where(ConFactSnapshot.tenant_id == tenant_id,
                                                 ConFactSnapshot.fact_snapshot_id == fact_snapshot_id)).scalar_one_or_none()
    return (s.facts, s.facts_hash) if s else ("MISSING", None)


def resolve_terms(db, tenant_id: str, netting_set_id: str, as_of: datetime, known_at: datetime | None = None,
                  fact_snapshot_id: str | None = None) -> dict:
    """Frozen contract v1. Never raises for business blocks: returns status BLOCKED with reasons."""
    base = {"status": "BLOCKED", "terms_hash": None, "terms": None, "blocking_reasons": [], "source_ids": [],
            "netting_set_id": netting_set_id, "as_of": iso(as_of), "fact_snapshot_id": fact_snapshot_id, "required_facts": [],
            "regulatory_constraints": "NOT_EVALUATED_HERE (A1 checks regulatory limits separately; conflicts escalate)"}
    pkg = db.execute(select(ConPackage).where(ConPackage.tenant_id == tenant_id,
                                              ConPackage.netting_set_id == netting_set_id)).scalar_one_or_none()
    if pkg is None:
        base["blocking_reasons"] = [f"NETTING_SET_UNKNOWN:{netting_set_id}"]
        return base
    vers = [v for v in _versions(db, pkg) if known_at is None or v.frozen_at <= known_at]
    if not vers:
        _, why, _, _ = _candidate(db, pkg)
        base["blocking_reasons"] = ["NO_FROZEN_TERMS"] + why
        return base
    ver = vers[-1]
    facts, fh = _snapshot_facts(db, tenant_id, fact_snapshot_id)
    if facts == "MISSING":
        base["blocking_reasons"] = [f"FACT_SNAPSHOT_NOT_FOUND:{fact_snapshot_id}"]
        return base
    values, blocking, need = T.resolve_entries(ver.entries, as_of, facts)
    base.update(version_id=ver.version_id, version_no=ver.version_no, required_facts=need or ver.required_facts)
    for req in T.REQUIRED_KEYS:
        if req not in values and not any(b.endswith(f":{req}") or f":{req}:" in b for b in blocking):
            blocking.append(f"NO_TERMS_EFFECTIVE_AT_AS_OF:{req}")
    if blocking:
        base["blocking_reasons"] = sorted(set(blocking))
        return base
    terms = T.terms_output(values, ver.pkg_static)
    h = content_hash({"bundle_hash": ver.bundle_hash, "netting_set_id": netting_set_id, "terms": terms, "facts_hash": fh})
    if db.get(ConResolution, (tenant_id, h)) is None:
        _insert_ignore(db, ConResolution, dict(tenant_id=tenant_id, terms_hash=h, netting_set_id=netting_set_id,
                                               version_id=ver.version_id, as_of=as_of, fact_snapshot_id=fact_snapshot_id,
                                               terms=terms))
    base.update(status="RESOLVED", terms_hash=h, terms=terms, blocking_reasons=[],
                source_ids=sorted({e["doc_sha256"] for e in ver.entries}), bundle_hash=ver.bundle_hash)
    return base


def _resolution_business_hash(res: dict) -> str:
    """Business hash of a resolution: the terms hash when RESOLVED, else a hash of the explicit blocking outcome."""
    if res["status"] == "RESOLVED":
        return res["terms_hash"]
    return content_hash({"status": "BLOCKED", "reasons": sorted(res["blocking_reasons"])})


def record_resolution(db, p: Principal, corr: str, netting_set_id: str, as_of: datetime, known_at, fact_snapshot_id) -> dict:
    """Audited resolution (evidence + replay spec). known_at is pinned (defaults to now) so the event stays replayable
    later even after newer versions are frozen (bitemporal; C-A08)."""
    _scope(p)
    _need_role(p, "maker", "checker", "compliance_officer", "tenant_admin")
    known = known_at or utcnow()
    res = resolve_terms(db, p.tenant_id, netting_set_id, as_of, known, fact_snapshot_id)
    pkg = db.execute(select(ConPackage).where(ConPackage.tenant_id == p.tenant_id,
                                              ConPackage.netting_set_id == netting_set_id)).scalar_one_or_none()
    bh = _resolution_business_hash(res)
    known_s = known.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    inputs = {"kind": "RESOLUTION", "netting_set_id": netting_set_id, "as_of": iso(as_of), "known_at": known_s,
              "fact_snapshot_id": fact_snapshot_id}
    ev = _ev(db, p, corr, "TERMS_RESOLVED" if res["status"] == "RESOLVED" else "TERMS_RESOLUTION_BLOCKED", "terms_version",
             res["terms_hash"] or bh, mode="MANUAL", effective_at=as_of, output_hash=bh,
             rationale="; ".join(res["blocking_reasons"]) or f"Resolved at as_of {iso(as_of)} (known_at pinned)",
             parents=_parents(("contract_terms", pkg.package_id)) if pkg is not None else None,
             replay=_replay_spec(bh, inputs),
             payload={"status": res["status"], "netting_set_id": netting_set_id, "version_no": res.get("version_no"),
                      "blocking_reasons": res["blocking_reasons"], "fact_snapshot_id": fact_snapshot_id})
    return {**res, "evidence_event_id": ev.event_id, "known_at": known_s}


def replay_contracts(session, tenant_id: str, spec: dict) -> dict:
    """replay.contracts: re-derive the business hash from PERSISTED inputs only (read-only; the caller rolls back).
    FROZEN_BUNDLE -> recompute the bundle hash from the stored version entries (detects any post-freeze alteration);
    RESOLUTION    -> re-resolve at the pinned as_of/known_at/fact snapshot (historical terms are reproduced even after
                     later amendments, because known_at pins the version)."""
    inp = spec.get("inputs") or {}
    out = {"engine_version": ENGINE_VERSION, "method_version": METHOD_VERSION}
    if inp.get("kind") == "FROZEN_BUNDLE":
        ver = session.get(ConVersion, inp.get("version_id"))
        if ver is None or ver.tenant_id != tenant_id:
            raise NotFound("Frozen version not found")
        static = {"master_id": ver.master_id, "csa_id": ver.source_ref, "netting_set_id": ver.netting_set_id, **ver.pkg_static}
        out["business_hash"] = _bundle_hash(static, ver.entries)
        return out
    if inp.get("kind") == "RESOLUTION":
        res = resolve_terms(session, tenant_id, inp["netting_set_id"], T.parse_dt(inp["as_of"]), T.parse_dt(inp["known_at"]),
                            inp.get("fact_snapshot_id"))
        out["business_hash"] = _resolution_business_hash(res)
        return out
    raise ValidationFailed("replay spec has unknown inputs.kind", field_errors={"inputs.kind": "unsupported"})


def terms_by_hash(db, tenant_id: str, terms_hash: str) -> dict | None:
    r = db.get(ConResolution, (tenant_id, terms_hash))
    return None if r is None else {"terms_hash": terms_hash, "terms": r.terms, "netting_set_id": r.netting_set_id,
                                   "version_id": r.version_id, "as_of": iso(r.as_of)}


# ----------------------------------------------------------------------------------------- netting
def register_trade(db, p: Principal, corr: str, trade_id: str, declared_master_id: str | None, declared_ns: str | None) -> dict:
    _scope(p)
    _need_role(p, "maker", "tenant_admin")
    if not trade_id or not trade_id.strip():
        raise ValidationFailed("trade_id required", field_errors={"trade_id": "required"})
    pkg = None
    if declared_ns:
        # Lock the agreement row: serialises with field approvals that change netting authorisation and with
        # concurrent registrations of trades into the same agreement.
        pkg = db.execute(lock_for_update(select(ConPackage).where(ConPackage.tenant_id == p.tenant_id,
                                                                  ConPackage.netting_set_id == declared_ns))
                         .execution_options(populate_existing=True)).scalar_one_or_none()
    ex = db.execute(select(ConNettingMembership).where(ConNettingMembership.tenant_id == p.tenant_id,
                                                       ConNettingMembership.trade_id == trade_id)
                    .execution_options(populate_existing=True)).scalar_one_or_none()
    if pkg is None:
        status, ns, master = "QUARANTINED", None, None
        reason = (f"UNKNOWN_MAPPING: netting set {declared_ns!r} is not a registered agreement" if declared_ns
                  else "UNKNOWN_MAPPING: trade declares no netting set")
    elif declared_master_id != pkg.master_id:
        status, ns, master = "REJECTED", None, None
        reason = (f"CROSS_MASTER: trade declares master {declared_master_id!r} but netting set {declared_ns} is governed by "
                  f"master {pkg.master_id}; cross-master netting is not permitted")
    else:
        authorised = db.execute(select(ConField).where(ConField.package_id == pkg.package_id, ConField.field_key == "netting_authorization",
                                                       ConField.status == "APPROVED")).first() is not None
        if not authorised:
            status, ns, master = "QUARANTINED", None, pkg.master_id
            reason = "NETTING_NOT_AUTHORISED: no approved close-out netting authorisation for this agreement yet"
        else:
            status, ns, master = "MEMBER", pkg.netting_set_id, pkg.master_id
            reason = (f"Trade declares master {pkg.master_id} and netting set {pkg.netting_set_id}; that netting set is governed by "
                      f"{pkg.csa_id} under {pkg.master_id} with approved close-out netting authorisation")
    def _same_or_conflict(row: ConNettingMembership) -> dict:
        if (row.status, row.netting_set_id, row.declared_master_id, row.declared_netting_set_id) == (status, ns, declared_master_id, declared_ns):
            return membership_out(row)
        raise Conflict("Trade already mapped differently; mapping changes require a reviewed amendment", code="MAPPING_CONFLICT")

    if ex is not None:
        return _same_or_conflict(ex)
    m = ConNettingMembership(tenant_id=p.tenant_id, trade_id=trade_id, declared_master_id=declared_master_id,
                             declared_netting_set_id=declared_ns, netting_set_id=ns, master_id=master, status=status,
                             reason=reason, recorded_by=p.user_id)
    try:
        with db.begin_nested():  # a concurrent registration of the same trade (e.g. unknown netting set) must not 500
            db.add(m)
            db.flush()
    except IntegrityError:
        won = db.execute(select(ConNettingMembership).where(ConNettingMembership.tenant_id == p.tenant_id,
                                                            ConNettingMembership.trade_id == trade_id)
                         .execution_options(populate_existing=True)).scalar_one()
        return _same_or_conflict(won)
    _ev(db, p, corr, "NETTING_MAPPING_RECORDED", "trade_mapping", trade_id, mode="MANUAL", rationale=reason,
        parents=_parents(("contract_package", pkg.package_id)) if pkg is not None else None,
        payload={"trade_id": trade_id, "status": status, "netting_set_id": ns, "declared_master_id": declared_master_id,
                 "declared_netting_set_id": declared_ns})
    return membership_out(m)


def membership_out(m: ConNettingMembership) -> dict:
    return {"trade_id": m.trade_id, "netting_set_id": m.netting_set_id, "master_id": m.master_id, "status": m.status,
            "reason": m.reason, "declared_master_id": m.declared_master_id, "declared_netting_set_id": m.declared_netting_set_id}


def netting_membership(db, tenant_id: str, trade_id: str, as_of: datetime | None = None) -> dict:
    """Frozen contract v1: {"netting_set_id"|None, "master_id", "reason"}; unknown/rejected mapping => None."""
    m = db.execute(select(ConNettingMembership).where(ConNettingMembership.tenant_id == tenant_id,
                                                      ConNettingMembership.trade_id == trade_id)).scalar_one_or_none()
    if m is None:
        return {"netting_set_id": None, "master_id": None, "status": "UNKNOWN", "reason": "UNKNOWN_MAPPING: trade not registered (quarantine)"}
    return {"netting_set_id": m.netting_set_id if m.status == "MEMBER" else None, "master_id": m.master_id,
            "status": m.status, "reason": m.reason}


# ----------------------------------------------------------------------------------------- facts
def record_facts(db, p: Principal, corr: str, facts: dict, fact_snapshot_id: str | None = None) -> dict:
    _scope(p)
    _need_role(p, "maker")
    fe, norm = {}, {}
    if not isinstance(facts, dict) or not facts:
        raise ValidationFailed("facts required", field_errors={"facts": "required"})
    for k, f in facts.items():
        if not isinstance(f, dict) or not f.get("source_id") or "value" not in f:
            fe[k] = "each fact needs value and an explicit source_id"
            continue
        try:
            norm[k] = {"value": f["value"], "source_id": f["source_id"], "effective_at": iso(T.parse_dt(f["effective_at"]))}
        except Exception:  # noqa: BLE001
            fe[k] = "effective_at must be an ISO-8601 datetime"
    if fe:
        raise ValidationFailed("Fact validation failed", field_errors=fe)
    sid = fact_snapshot_id or new_id()
    if db.execute(select(ConFactSnapshot).where(ConFactSnapshot.tenant_id == p.tenant_id,
                                                ConFactSnapshot.fact_snapshot_id == sid)).first():
        raise Conflict("fact snapshot id already exists (snapshots are immutable)", code="FACT_SNAPSHOT_EXISTS")
    h = content_hash(norm)
    try:
        with db.begin_nested():
            db.add(ConFactSnapshot(tenant_id=p.tenant_id, fact_snapshot_id=sid, facts=norm, facts_hash=h, created_by=p.user_id))
            db.flush()
    except IntegrityError:
        raise Conflict("fact snapshot id already exists (snapshots are immutable)", code="FACT_SNAPSHOT_EXISTS") from None
    _ev(db, p, corr, "FACT_SNAPSHOT_RECORDED", "fact_snapshot", sid, mode="MANUAL", output_hash=h,
        payload={"fact_keys": sorted(norm), "sources": sorted({f["source_id"] for f in norm.values()})})
    return {"fact_snapshot_id": sid, "facts_hash": h, "facts": norm}


# ----------------------------------------------------------------------------------------- read models
_STATUSES = ("PROPOSED", "UNRESOLVED", "APPROVED", "REJECTED")


def _bulk_summary_data(db, pkgs: list[ConPackage]) -> dict[str, dict]:
    """Grouped queries for a page of packages (no per-row queries, and no document text is loaded).
    Found by the PostgreSQL benchmark: the per-package version cost 635 ms p50 at 120 agreements."""
    out = {p.package_id: {"counts": dict.fromkeys(_STATUSES, 0), "docs": 0, "missing": 0, "pending": 0, "latest": None, "members": 0}
           for p in pkgs}
    for i in range(0, len(pkgs), 400):  # chunked: SQLite caps bound variables
        chunk = pkgs[i:i + 400]
        ids = [p.package_id for p in chunk]
        for pid, st, n in db.execute(select(ConField.package_id, ConField.status, func.count()).where(ConField.package_id.in_(ids))
                                     .group_by(ConField.package_id, ConField.status)):
            out[pid]["counts"][st] = n
        for pid, missing, mode in db.execute(select(ConDocument.package_id, ConDocument.missing_annexes, ConDocument.extracted_mode)
                                             .where(ConDocument.package_id.in_(ids))):
            o = out[pid]
            o["docs"] += 1
            o["missing"] += 1 if missing else 0
            o["pending"] += 1 if mode is None else 0
        for pid, v in db.execute(select(ConVersion.package_id, func.max(ConVersion.version_no)).where(ConVersion.package_id.in_(ids))
                                 .group_by(ConVersion.package_id)):
            out[pid]["latest"] = v
        by_ns = {p.netting_set_id: p.package_id for p in chunk}
        for ns, n in db.execute(select(ConNettingMembership.netting_set_id, func.count()).where(
                ConNettingMembership.tenant_id == chunk[0].tenant_id, ConNettingMembership.status == "MEMBER",
                ConNettingMembership.netting_set_id.in_(list(by_ns))).group_by(ConNettingMembership.netting_set_id)):
            out[by_ns[ns]]["members"] = n
    return out


def package_summary(db, pkg: ConPackage, pre: dict | None = None) -> dict:
    if pre is None:
        pre = _bulk_summary_data(db, [pkg])[pkg.package_id]
    counts, members = pre["counts"], pre["members"]
    return {"package_id": pkg.package_id, "master_id": pkg.master_id, "csa_id": pkg.csa_id, "netting_set_id": pkg.netting_set_id,
            "party_a_id": pkg.party_a_id, "party_b_id": pkg.party_b_id, "party_a_name": pkg.party_a_name,
            "party_b_name": pkg.party_b_name, "base_currency": pkg.base_currency, "governing_law": pkg.governing_law,
            "status": pkg.status, "revision": pkg.revision, "documents": pre["docs"],
            "docs_with_missing_annexes": pre["missing"],
            "docs_pending_extraction": pre["pending"],
            "fields": counts, "outstanding_review": counts["PROPOSED"] + counts["UNRESOLVED"],
            "latest_version_no": pre["latest"], "trades_in_netting_set": members,
            "created_at": pkg.created_at.isoformat(), "provenance": pkg.provenance}


def package_detail(db, tenant_id: str, package_id: str) -> dict:
    pkg = get_package(db, tenant_id, package_id)
    o = package_summary(db, pkg)
    o["document_list"] = [{"doc_id": d.doc_id, "kind": d.kind, "title": d.title, "doc_ref": d.doc_ref, "sha256": d.sha256,
                           "effective_from": iso(d.effective_from), "rank": d.precedence_rank, "missing_annexes": d.missing_annexes,
                           "extracted_mode": d.extracted_mode, "chars": len(d.text)} for d in _docs(db, pkg)]
    o["versions"] = [version_out(v) for v in _versions(db, pkg)]
    return o


def list_packages(db, tenant_id: str, status: str | None = None, currency: str | None = None, counterparty: str | None = None,
                  needs_review: bool | None = None, limit: int = 50, cursor: str | None = None) -> dict:
    limit = max(1, min(limit, 200))
    q = select(ConPackage).where(ConPackage.tenant_id == tenant_id)
    if status:
        q = q.where(ConPackage.status == status)
    if currency:
        q = q.where(ConPackage.base_currency == currency)
    if counterparty:
        q = q.where((ConPackage.party_a_id == counterparty) | (ConPackage.party_b_id == counterparty))
    if cursor:
        q = q.where(ConPackage.package_id > cursor)
    rows = list(db.execute(q.order_by(ConPackage.package_id)).scalars())
    pre = _bulk_summary_data(db, rows) if rows else {}
    items = []
    for r in rows:
        s = package_summary(db, r, pre[r.package_id])
        if needs_review is not None and (s["outstanding_review"] > 0) != needs_review:
            continue
        items.append(s)
        if len(items) > limit:
            break
    nxt = items[limit - 1]["package_id"] if len(items) > limit else None
    return {"items": items[:limit], "next_cursor": nxt}


def terms_view(db, tenant_id: str, package_id: str) -> dict:
    pkg = get_package(db, tenant_id, package_id)
    docs = {d.doc_id: d for d in _docs(db, pkg)}
    return {"package": package_summary(db, pkg), "fields": [field_out(f, docs.get(f.doc_id)) for f in _fields(db, pkg)],
            "readiness": readiness(db, tenant_id, package_id),
            "eligibility": [{k: getattr(r, k) for k in ("field_id", "asset_class", "currency", "issuer_class", "valuation_pct",
                                                        "haircut_pct", "fx_haircut_pct", "concentration_cap_pct", "cap_denominator",
                                                        "min_rating", "max_maturity_years", "field_status")}
                            for r in db.execute(select(ConEligibility).where(ConEligibility.package_id == pkg.package_id)
                                                .order_by(ConEligibility.row_id)).scalars()]}


def document_view(db, tenant_id: str, package_id: str, doc_id: str) -> dict:
    pkg = get_package(db, tenant_id, package_id)
    d = db.execute(select(ConDocument).where(ConDocument.doc_id == doc_id, ConDocument.package_id == pkg.package_id)).scalar_one_or_none()
    if d is None:
        raise NotFound("Document not found")
    return {"doc_id": d.doc_id, "doc_ref": d.doc_ref, "kind": d.kind, "sha256": d.sha256, "text": d.text,
            "spans": [{"field_id": f.field_id, "field_key": f.field_key, "start": f.start_char, "end": f.end_char, "status": f.status}
                      for f in _fields(db, pkg) if f.doc_id == d.doc_id], "missing_annexes": d.missing_annexes}


def list_versions(db, tenant_id: str, package_id: str, with_entries: bool = False) -> dict:
    pkg = get_package(db, tenant_id, package_id)
    return {"items": [version_out(v, entries=with_entries) for v in _versions(db, pkg)]}


def compare_versions(db, tenant_id: str, package_id: str, a_id: str, b_id: str, as_of: datetime) -> dict:
    pkg = get_package(db, tenant_id, package_id)
    vs = {v.version_id: v for v in _versions(db, pkg)}
    if a_id not in vs or b_id not in vs:
        raise NotFound("Version not found")
    a, b = vs[a_id], vs[b_id]

    def sig(e):
        return json.dumps({k: e[k] for k in ("key", "direction", "value", "condition", "effective_from", "rank", "doc_sha256")},
                          sort_keys=True, default=str)
    sa, sb = {sig(e): e for e in a.entries}, {sig(e): e for e in b.entries}
    va, _, _ = T.resolve_entries(a.entries, as_of, None)
    vb, _, _ = T.resolve_entries(b.entries, as_of, None)
    changed = [{"key": k, "from": va.get(k), "to": vb.get(k)} for k in sorted(set(va) | set(vb)) if va.get(k) != vb.get(k)]
    n = db.execute(select(func.count()).select_from(ConNettingMembership).where(
        ConNettingMembership.tenant_id == tenant_id, ConNettingMembership.netting_set_id == pkg.netting_set_id,
        ConNettingMembership.status == "MEMBER")).scalar_one()
    return {"a": version_out(a), "b": version_out(b), "as_of": iso(as_of),
            "added_entries": [e for s, e in sb.items() if s not in sa], "removed_entries": [e for s, e in sa.items() if s not in sb],
            "resolved_changes_at_as_of": changed,
            "affected_netting_sets": [{"netting_set_id": pkg.netting_set_id, "master_id": pkg.master_id, "member_trades": n}]}


def netting_map(db, tenant_id: str, package_id: str) -> dict:
    pkg = get_package(db, tenant_id, package_id)
    rows = db.execute(select(ConNettingMembership).where(ConNettingMembership.tenant_id == tenant_id,
                                                         ConNettingMembership.declared_netting_set_id == pkg.netting_set_id)
                      .order_by(ConNettingMembership.trade_id)).scalars()
    return {"netting_set_id": pkg.netting_set_id, "master_id": pkg.master_id, "items": [membership_out(m) for m in rows]}


def quarantine_list(db, tenant_id: str) -> dict:
    rows = db.execute(select(ConNettingMembership).where(ConNettingMembership.tenant_id == tenant_id,
                                                         ConNettingMembership.status != "MEMBER")
                      .order_by(ConNettingMembership.trade_id)).scalars()
    return {"items": [membership_out(m) for m in rows]}


# --------------------------------------------------------------------------------- registry of public services
def _svc_resolve_terms(session, tenant_id, netting_set_id, as_of, known_at=None, fact_snapshot_id=None):
    return resolve_terms(session, tenant_id, netting_set_id, as_of, known_at, fact_snapshot_id)


services.register("contracts.resolve_terms", _svc_resolve_terms)
services.register("contracts.netting_membership", lambda session, tenant_id, trade_id, as_of=None: netting_membership(session, tenant_id, trade_id, as_of))
services.register("contracts.terms_by_hash", lambda session, tenant_id, terms_hash: terms_by_hash(session, tenant_id, terms_hash))
services.register("replay.contracts", replay_contracts)

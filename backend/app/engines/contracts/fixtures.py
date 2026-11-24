"""Synthetic contract fixtures + seed. Seeding uses the SAME workflow as users (create -> extract -> maker/checker review
-> propose -> freeze) with two distinct seed actors, so seeded terms carry real evidence and approvals.
Seed actors are labelled `seed-maker` / `seed-checker`. All documents are original and fictional."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from app.auth.security import Principal
from app.core.config import REPO_ROOT
from app.db.base import new_id
from app.engines.contracts import services as S
from app.engines.contracts.models import ConField, ConPackage

DOC_DIR = REPO_ROOT / "fixtures" / "contracts" / "documents"
MER = ("ENT-MERIDIAN", "Meridian Demo Bank")
NWD = ("ENT-NORTHWIND", "Northwind Demo Clearing")
ORC = ("ENT-OTHERCP", "Orchid Demo Counterparty")

DEFAULT_TRADE_MAP = [  # (trade_id, declared_master, declared_netting_set); expected status in comment
    ("TRD-G09-001", "MA-MER-NWD-001", "NS-MER-NWD-001"),  # MEMBER
    ("TRD-G09-002", "MA-MER-OTH-002", "NS-MER-OTH-002"),  # MEMBER (separate set: no offset against 001)
    ("TRD-G09-X", "MA-MER-OTH-002", "NS-MER-NWD-001"),    # REJECTED cross-master
    ("TRD-UNKNOWN-NS", "MA-MER-NWD-001", "NS-NOT-REGISTERED"),  # QUARANTINED unknown mapping
]


def load_doc(name: str) -> str:
    """Fixture documents are read with CRLF normalised so SHA-256 is stable across checkouts."""
    return (DOC_DIR / name).read_bytes().replace(b"\r\n", b"\n").decode("utf-8")


def _actor(tenant_id: str, uid: str, role: str) -> Principal:
    return Principal(uid, tenant_id, uid, uid, (role,), (), "")


def _spec(master, csa, ns, a, b, docs, mode, freeze=True, amendment=None):
    return dict(master_id=master, csa_id=csa, netting_set_id=ns, party_a=a, party_b=b, docs=docs, mode=mode, freeze=freeze,
                amendment=amendment)


SPECS = [
    _spec("MA-MER-NWD-001", "CSA-MER-NWD-001", "NS-MER-NWD-001", MER, NWD,
          [("MASTER", "MA-MER-NWD-001", "master_ma_mer_nwd_001.txt"), ("CSA", "CSA-MER-NWD-001", "csa_mer_nwd_001.txt")], "REPLAY",
          amendment=("AMD-001 to CSA-MER-NWD-001", "amendment_001_csa_mer_nwd_001.txt", "2026-09-01T00:00:00Z")),
    _spec("MA-MER-OTH-002", "CSA-MER-OTH-002", "NS-MER-OTH-002", MER, ORC, [("CSA", "CSA-MER-OTH-002", "csa_mer_oth_002.txt")], "RULES"),
    _spec("MA-DEMO-G25", "CSA-DEMO-G25", "NS-DEMO-G25", MER, ORC, [("CSA", "CSA-DEMO-G25", "csa_demo_g25.txt")], "RULES"),
    _spec("MA-DEMO-G14R", "CSA-DEMO-G14R", "NS-DEMO-G14-RATING", MER, ORC, [("CSA", "CSA-DEMO-G14R", "csa_demo_g14_rating.txt")], "RULES"),
    # G14 missing MTA: extracted and approved but deliberately NOT freezable (required MTA absent)
    _spec("MA-DEMO-G14M", "CSA-DEMO-G14M", "NS-DEMO-G14-NOMTA", MER, ORC, [("CSA", "CSA-DEMO-G14M", "csa_demo_g14_nomta.txt")], "RULES",
          freeze=False),
]


def package_body(spec: dict) -> dict:
    kinds = {"MASTER": 0, "CSA": 1}
    return {"master_id": spec["master_id"], "csa_id": spec["csa_id"], "netting_set_id": spec["netting_set_id"],
            "party_a_id": spec["party_a"][0], "party_a_name": spec["party_a"][1], "party_b_id": spec["party_b"][0],
            "party_b_name": spec["party_b"][1], "base_currency": "USD", "governing_law": "Fictional Demo Law (synthetic)",
            "documents": [{"kind": k, "doc_ref": ref, "title": ref, "text": load_doc(f), "effective_from": "2026-01-01T00:00:00Z"}
                          for k, ref, f in spec["docs"] if k in kinds]}


def _approve_all(db, checker: Principal, corr: str, package_id: str) -> int:
    n = 0
    for f in db.execute(select(ConField).where(ConField.package_id == package_id, ConField.status == "PROPOSED")).scalars().all():
        S.review_field(db, checker, corr, package_id, f.field_id, "APPROVE", "Seed fixture: verified against quoted source text",
                       f.revision)
        n += 1
    return n


def _freeze(db, maker, checker, corr, package_id) -> dict:
    ap = S.propose_freeze(db, maker, corr, package_id, "Seed fixture: propose freeze of approved terms")
    return S.freeze(db, checker, corr, package_id, ap["approval_id"], "Seed fixture: independent checker approves exact terms hash")


def seed_fixtures(session, tenant_id: str, trade_map=None) -> dict:
    """Idempotent. Creates the shared-ID agreement packages for tenant `tenant_id`."""
    maker, checker = _actor(tenant_id, "seed-maker", "maker"), _actor(tenant_id, "seed-checker", "checker")
    out: dict = {"packages": {}, "facts": [], "trades": []}
    for spec in SPECS:
        if session.execute(select(ConPackage).where(ConPackage.tenant_id == tenant_id,
                                                    ConPackage.netting_set_id == spec["netting_set_id"])).scalar_one_or_none():
            out["packages"][spec["netting_set_id"]] = "EXISTS"
            continue
        corr = new_id()
        pkg = S.create_package(session, maker, corr, package_body(spec))
        S.extract(session, maker, corr, pkg["package_id"], spec["mode"])
        _approve_all(session, checker, corr, pkg["package_id"])
        res = {"package_id": pkg["package_id"]}
        if spec["freeze"]:
            res["v1"] = _freeze(session, maker, checker, corr, pkg["package_id"])["bundle_hash"]
        if spec.get("amendment"):
            ref, fname, eff = spec["amendment"]
            S.add_document(session, maker, corr, pkg["package_id"],
                           {"doc_ref": ref, "title": ref, "text": load_doc(fname), "effective_from": eff}, amendment=True)
            S.extract(session, maker, corr, pkg["package_id"], "REPLAY")
            _approve_all(session, checker, corr, pkg["package_id"])
            res["v2"] = _freeze(session, maker, checker, corr, pkg["package_id"])["bundle_hash"]
        out["packages"][spec["netting_set_id"]] = res
    for sid, rating in (("FACT-RATING-BBB", "BBB"), ("FACT-RATING-BB", "BB"), ("FACT-RATING-FUTURE", "BB")):
        try:
            eff = "2026-12-31T00:00:00Z" if sid == "FACT-RATING-FUTURE" else "2026-06-01T00:00:00Z"
            S.record_facts(session, maker, new_id(), {"rating:ENT-OTHERCP": {"value": rating, "source_id": f"SYNTH-RATING-{rating}",
                                                                              "effective_at": eff}}, fact_snapshot_id=sid)
            out["facts"].append(sid)
        except Exception as e:  # noqa: BLE001 - only the idempotent 'exists' conflict is expected
            if getattr(e, "code", "") != "FACT_SNAPSHOT_EXISTS":
                raise
    for t in (trade_map if trade_map is not None else DEFAULT_TRADE_MAP):
        out["trades"].append(S.register_trade(session, maker, new_id(), *t))
    return out

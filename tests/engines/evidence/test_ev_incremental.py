"""Incremental checkpoint creation / anchored verification (A6 round 2) and its documented trust assumption."""
import pytest
from sqlalchemy import text

from app.core.errors import ValidationFailed
from app.db.base import SessionLocal
from app.engines.evidence import checkpoints as cps
from app.engines.evidence import service as ev
from tests.engines.evidence.conftest import add_events


def _seed(tenant, n):
    with SessionLocal() as s:
        add_events(s, tenant, n)
        s.commit()


def test_incremental_checkpoint_recomputes_only_new_events(tenant):
    _seed(tenant, 20)
    with SessionLocal() as s:
        c1 = cps.create_checkpoint(s, tenant)                      # no trusted anchor yet -> FULL
        s.commit()
    assert c1["verification"]["mode"] == "FULL" and c1["verification"]["events_recomputed"] == 20
    _seed(tenant, 7)
    with SessionLocal() as s:
        c2 = cps.create_checkpoint(s, tenant)                      # incremental: only the 7 new events
        s.commit()
    assert c2["verification"] == {"mode": "INCREMENTAL", "events_recomputed": 7}
    assert c2["sequence"] == 27
    with SessionLocal() as s:
        full = cps.create_checkpoint(s, tenant, mode="full")       # explicit full pass is still available
        s.commit()
    assert full["verification"] == {"mode": "FULL", "events_recomputed": 27}


def test_incremental_and_full_agree_on_a_valid_chain(tenant):
    _seed(tenant, 12)
    with SessionLocal() as s:
        cps.create_checkpoint(s, tenant)
        s.commit()
    _seed(tenant, 5)
    with SessionLocal() as s:
        f = cps.verify_against_exported(s, tenant, mode="full")
        i = cps.verify_against_exported(s, tenant, mode="incremental")
    assert f["valid"] and i["valid"] and f["head_hash"] == i["head_hash"] and f["head_sequence"] == i["head_sequence"] == 17
    assert f["mode"] == "FULL" and i["mode"] == "INCREMENTAL" and i["events_recomputed"] == 5 and f["events_recomputed"] == 17
    assert "NOT re-hashed" in i["trust_assumption"]


def test_incremental_detects_tamper_in_new_segment_and_refuses_to_sign(tenant):
    _seed(tenant, 6)
    with SessionLocal() as s:
        cps.create_checkpoint(s, tenant)
        s.commit()
    _seed(tenant, 4)
    with SessionLocal() as s:
        s.execute(text("UPDATE ev_events SET action='TAMPERED' WHERE tenant_id=:t AND sequence=8"), {"t": tenant})
        s.commit()
    with SessionLocal() as s:
        r = cps.verify_against_exported(s, tenant, mode="incremental")
        assert not r["valid"] and any(p["problem"] == "EVENT_HASH_MISMATCH" and p["sequence"] == 8 for p in r["problems"])
        with pytest.raises(ValidationFailed):
            cps.create_checkpoint(s, tenant)                        # incremental creation must not bless it


def test_documented_limit_incremental_does_not_rehash_below_anchor_but_full_does(tenant):
    """Honest trade-off: row-content edits at/below the previous anchor are only visible to a FULL pass."""
    _seed(tenant, 8)
    with SessionLocal() as s:
        cps.create_checkpoint(s, tenant)
        s.commit()
    _seed(tenant, 3)
    with SessionLocal() as s:
        s.execute(text("UPDATE ev_events SET action='TAMPERED' WHERE tenant_id=:t AND sequence=3"), {"t": tenant})
        s.commit()
    with SessionLocal() as s:
        assert cps.verify_against_exported(s, tenant, mode="incremental")["valid"] is True   # documented blind spot
        full = cps.verify_against_exported(s, tenant, mode="full")
        assert not full["valid"] and any(p["problem"] == "EVENT_HASH_MISMATCH" and p["sequence"] == 3 for p in full["problems"])


def test_incremental_detects_truncation_below_the_anchor(tenant):
    _seed(tenant, 9)
    with SessionLocal() as s:
        cps.create_checkpoint(s, tenant)
        s.commit()
    with SessionLocal() as s:  # privileged truncation of the tail + head rewrite, chain remains internally consistent
        s.execute(text("DELETE FROM ev_events WHERE tenant_id=:t AND sequence>6"), {"t": tenant})
        s.execute(text("UPDATE ev_chain_heads SET sequence=6, head_hash=(SELECT event_hash FROM ev_events "
                       "WHERE tenant_id=:t AND sequence=6) WHERE tenant_id=:t"), {"t": tenant})
        s.commit()
    with SessionLocal() as s:
        assert ev.verify_chain(s, tenant)["valid"] is True          # bare chain cannot see it
        r = cps.verify_against_exported(s, tenant, mode="incremental")
        assert not r["valid"]
        assert {p["problem"] for p in r["problems"]} & {"INCREMENTAL_BASE_NOT_REPRODUCED", "CHECKPOINT_NOT_REPRODUCED", "CHAIN_SHORTER_THAN_ANCHOR"}


def test_unknown_mode_rejected(tenant):
    _seed(tenant, 2)
    with SessionLocal() as s:
        with pytest.raises(ValidationFailed):
            cps.create_checkpoint(s, tenant, mode="sometimes")
        with pytest.raises(ValidationFailed):
            cps.verify_against_exported(s, tenant, mode="sometimes")

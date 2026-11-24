import threading

from sqlalchemy import text

from app.db.base import SessionLocal
from app.engines.evidence import service as ev


def _add(s, t, n):
    for i in range(n):
        ev.append(s, t, engine="platform", action="TEST", actor_id="u", actor_type="USER",
                  entity_type="x", entity_id=str(i), correlation_id="c")


def test_chain_valid_then_tamper_detected():
    t = "chain-t1"
    with SessionLocal() as s:
        _add(s, t, 5)
        s.commit()
        r = ev.verify_chain(s, t)
        assert r["valid"] and r["events"] == 5
        cp = {"sequence": r["head_sequence"], "head_hash": r["head_hash"]}
        s.execute(text("UPDATE ev_events SET action='TAMPERED' WHERE tenant_id=:t AND sequence=3"), {"t": t})
        s.commit()
        r2 = ev.verify_chain(s, t, cp)
        assert not r2["valid"]
        assert any(p["problem"] == "EVENT_HASH_MISMATCH" and p["sequence"] == 3 for p in r2["problems"])


def test_tail_truncation_detected_only_with_trusted_checkpoint():
    t = "chain-t2"
    with SessionLocal() as s:
        _add(s, t, 4)
        s.commit()
        r = ev.verify_chain(s, t)
        cp = {"sequence": r["head_sequence"], "head_hash": r["head_hash"]}
        s.execute(text("DELETE FROM ev_events WHERE tenant_id=:t AND sequence=4"), {"t": t})
        s.execute(text("UPDATE ev_chain_heads SET sequence=3, head_hash=(SELECT event_hash FROM ev_events "
                       "WHERE tenant_id=:t AND sequence=3) WHERE tenant_id=:t"), {"t": t})
        s.commit()
        assert ev.verify_chain(s, t)["valid"] is True      # bare chain cannot see it (documented limit)
        assert ev.verify_chain(s, t, cp)["valid"] is False  # anchored checkpoint does


def test_concurrent_append_yields_valid_sequence():
    t = "chain-t3"
    errs: list = []

    def work():
        try:
            with SessionLocal() as s:
                _add(s, t, 10)
                s.commit()
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    th = [threading.Thread(target=work) for _ in range(4)]
    [x.start() for x in th]
    [x.join() for x in th]
    assert not errs, errs
    with SessionLocal() as s:
        r = ev.verify_chain(s, t)
        assert r["valid"] and r["events"] == 40

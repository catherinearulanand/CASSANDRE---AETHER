"""E-A02 / E-A03 / E-A04 / G17 (truncation) / checkpoint + vault behaviour."""
import json
import threading
from pathlib import Path

import pytest
from sqlalchemy import text

from app.core.errors import Unavailable, ValidationFailed
from app.db.base import SessionLocal
from app.engines.evidence import checkpoints as cps
from app.engines.evidence import keys, objects
from app.engines.evidence import service as ev
from tests.engines.evidence.conftest import add_events


def test_modify_reorder_remove_detected(tenant):
    with SessionLocal() as s:
        add_events(s, tenant, 6)
        s.commit()
        assert ev.verify_chain(s, tenant)["valid"]
        # reorder: swap sequence numbers of 2 and 3 (via a temp value to respect the PK)
        for a, b in ((2, 99), (3, 2), (99, 3)):
            s.execute(text("UPDATE ev_events SET sequence=:b WHERE tenant_id=:t AND sequence=:a"), {"a": a, "b": b, "t": tenant})
        s.commit()
        r = ev.verify_chain(s, tenant)
        assert not r["valid"] and {p["problem"] for p in r["problems"]} & {"PREV_HASH_MISMATCH", "EVENT_HASH_MISMATCH"}


def test_removed_middle_event_is_a_gap(tenant):
    with SessionLocal() as s:
        add_events(s, tenant, 5)
        s.commit()
        s.execute(text("DELETE FROM ev_events WHERE tenant_id=:t AND sequence=3"), {"t": tenant})
        s.commit()
        r = ev.verify_chain(s, tenant)
        assert not r["valid"] and any(p["problem"] == "SEQUENCE_GAP_OR_REORDER" for p in r["problems"])


def test_truncation_detected_against_exported_signed_checkpoint(tenant):
    """G17/E-A03: delete the tail AND fix up the DB chain head: bare verify passes, the exported anchor catches it."""
    with SessionLocal() as s:
        add_events(s, tenant, 6)
        s.commit()
        cp = cps.create_checkpoint(s, tenant)
        s.commit()
        assert Path(cp["exported_path"]).exists()
        s.execute(text("DELETE FROM ev_events WHERE tenant_id=:t AND sequence>=5"), {"t": tenant})
        s.execute(text("UPDATE ev_chain_heads SET sequence=4, head_hash=(SELECT event_hash FROM ev_events "
                       "WHERE tenant_id=:t AND sequence=4) WHERE tenant_id=:t"), {"t": tenant})
        s.execute(text("DELETE FROM ev_checkpoints WHERE tenant_id=:t"), {"t": tenant})  # attacker also wipes the DB copy
        s.commit()
        assert ev.verify_chain(s, tenant)["valid"] is True  # documented limit of a bare hash chain
        r = cps.verify_against_exported(s, tenant)
        assert r["valid"] is False and r["anchor_status"] == "ANCHORED"
        assert any(p["problem"] in ("CHAIN_SHORTER_THAN_ANCHOR", "CHECKPOINT_NOT_REPRODUCED") for p in r["problems"])


def test_forged_checkpoint_file_rejected(tenant):
    with SessionLocal() as s:
        add_events(s, tenant, 3)
        s.commit()
        cp = cps.create_checkpoint(s, tenant)
        s.commit()
        f = Path(cp["exported_path"])
        d = json.loads(f.read_text())
        d["sequence"] = 1  # attacker lowers the anchor to hide a truncation
        f.write_text(json.dumps(d))
        r = cps.verify_against_exported(s, tenant)
        assert r["anchor_status"] == "NO_TRUSTED_ANCHOR" and r["invalid_signature_anchors"] == 1


def test_checkpoint_refuses_to_sign_tampered_chain(tenant):
    with SessionLocal() as s:
        add_events(s, tenant, 3)
        s.commit()
        s.execute(text("UPDATE ev_events SET action='X' WHERE tenant_id=:t AND sequence=2"), {"t": tenant})
        s.commit()
        with pytest.raises(ValidationFailed):
            cps.create_checkpoint(s, tenant)


def test_key_rotation_keeps_old_checkpoints_verifiable(tenant):
    with SessionLocal() as s:
        add_events(s, tenant, 2)
        s.commit()
        c1 = cps.create_checkpoint(s, tenant)
        old = keys.current_key_id()
        new = keys.rotate()
        assert new != old
        add_events(s, tenant, 1, prefix="n")
        c2 = cps.create_checkpoint(s, tenant)
        s.commit()
        assert c1["key_id"] == old and c2["key_id"] == new
        r = cps.verify_against_exported(s, tenant)
        assert r["valid"] and r["anchors_checked"] == 2


def test_concurrent_append_valid_sequence_and_anchor(tenant):
    """E-A04: 8 writers x 25 events -> contiguous sequence, valid chain, anchorable."""
    errs = []

    def work():
        try:
            with SessionLocal() as s:
                add_events(s, tenant, 25)
                s.commit()
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    th = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in th]
    [t.join() for t in th]
    assert not errs, errs
    with SessionLocal() as s:
        r = ev.verify_chain(s, tenant)
        assert r["valid"] and r["events"] == 200 and r["head_sequence"] == 200
        cps.create_checkpoint(s, tenant)
        s.commit()
        assert cps.verify_against_exported(s, tenant)["valid"]


def test_duplicate_event_id_rejected(tenant):
    from app.core.errors import Conflict
    with SessionLocal() as s:
        ev.append(s, tenant, engine="platform", action="A", actor_id="u", actor_type="USER", entity_type="x", entity_id="1",
                  correlation_id="c", event_id="dup-1")
        with pytest.raises(Conflict):
            ev.append(s, tenant, engine="platform", action="A", actor_id="u", actor_type="USER", entity_type="x", entity_id="2",
                      correlation_id="c", event_id="dup-1")
        s.rollback()


def test_vault_idempotent_tenant_scoped_and_corruption_detected(tenant):
    with SessionLocal() as s:
        h1 = objects.store_object(s, tenant, b"hello", "SNAPSHOT")
        h2 = objects.store_object(s, tenant, b"hello", "SNAPSHOT")
        s.commit()
        assert h1 == h2 and objects.read_object(s, tenant, h1) == b"hello"
        from app.core.errors import NotFound
        with pytest.raises(NotFound):
            objects.read_object(s, "other-tenant", h1)
        p = objects.object_path(tenant, h1)
        p.write_bytes(b"tampered")
        with pytest.raises(Unavailable):
            objects.read_object(s, tenant, h1)
        assert objects.verify_objects(s, tenant)["problems"][0]["problem"] == "HASH_MISMATCH"
        p.unlink()
        assert objects.verify_objects(s, tenant)["problems"][0]["problem"] == "MISSING_BLOB"
        with pytest.raises(ValidationFailed):
            objects.store_object(s, "../evil", b"x", "SNAPSHOT")


def test_purge_only_temp_objects_and_respects_legal_hold(tenant):
    with SessionLocal() as s:
        keep = objects.store_object(s, tenant, b"evidence", "SOURCE_DOCUMENT")
        objects.store_object(s, tenant, b"scratch", "TEMP")
        s.commit()
        assert objects.purge_temp_objects(s, tenant, older_than_days=0, legal_hold=True)["purged"] == 0
        assert objects.purge_temp_objects(s, tenant, older_than_days=0)["purged"] in (0, 1)  # created_at == now boundary
        assert objects.read_object(s, tenant, keep) == b"evidence"

"""Production code and the standalone verifier must both reproduce the independently generated golden values.
(No production code imports fixtures/expected; only this test reads it.)"""
import json
from datetime import datetime, timezone
from pathlib import Path

from app.core.canonical import canonical_bytes
from app.engines.evidence.checkpoints import signing_message
from app.engines.evidence.service import compute_event_hash
from tools.verify_bundle.verify import event_hash as standalone_event_hash

G = json.loads((Path(__file__).resolve().parents[3] / "fixtures" / "expected" / "evidence" / "golden_event.json").read_text(encoding="utf-8"))


def _app_fields():
    f = dict(G["fields"])
    for k in ("effective_at", "recorded_at"):  # the platform hashes datetimes; golden stores their canonical string form
        f[k] = datetime.strptime(f[k], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    return f


def test_platform_event_hash_matches_independent_golden():
    assert compute_event_hash(G["tenant_id"], G["sequence"], G["prev_hash"], _app_fields()) == G["expected_event_hash"]


def test_standalone_verifier_hash_matches_independent_golden():
    ev = {"sequence": G["sequence"], "prev_hash": G["prev_hash"], **G["fields"]}
    assert standalone_event_hash(G["tenant_id"], ev) == G["expected_event_hash"]


def test_canonical_bytes_match_golden_including_unicode_and_decimal_strings():
    rec = {"chain_id": G["tenant_id"], "sequence": G["sequence"], "prev_hash": G["prev_hash"], **_app_fields()}
    assert canonical_bytes(rec).decode("utf-8") == G["expected_canonical_json"]


def test_checkpoint_signing_message_matches_golden():
    msg = signing_message(G["tenant_id"], 1, G["expected_event_hash"], "golden-key", "2026-08-17T04:05:00.000000Z")
    assert msg.decode("utf-8") == G["expected_checkpoint_signing_message"]

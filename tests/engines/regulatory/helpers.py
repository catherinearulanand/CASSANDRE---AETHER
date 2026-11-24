import json
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from app.auth.security import Principal
from app.core import services
from app.engines.regulatory import registry
from app.engines.regulatory.models import RegSource

EXPECTED = json.loads((Path(__file__).resolve().parents[3] / "fixtures" / "expected" / "regulatory" / "golden.json").read_text(encoding="utf-8"))
SCOPE = {"all": [{"eq": ["market_type", "ETD_DOMESTIC"]}, {"eq": ["venue", "DEMO_NSE"]}]}
CTX = {"market_type": "ETD_DOMESTIC", "venue": "DEMO_NSE"}
TEXT = "Preamble.\nThe member shall hold 100 lots. Ünïcode ✓ clause: the broker shall not exceed 7 lots.\n"


def P(uid: str, tenant: str, role: str) -> Principal:
    return Principal(uid, tenant, uid, uid, (role,), (), "")


def dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)


@contextmanager
def fake_services(**fns):
    old = {k: services._REGISTRY.get(k) for k in fns}
    services._REGISTRY.update({k: v for k, v in fns.items() if v is not None})
    for k, v in fns.items():
        if v is None:
            services._REGISTRY.pop(k, None)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                services._REGISTRY.pop(k, None)
            else:
                services._REGISTRY[k] = v


class Env:
    def __init__(self, db):
        self.db = db
        self.tenant = f"t-{uuid.uuid4().hex[:10]}"
        self.maker, self.checker, self.checker2 = P("maker-1", self.tenant, "maker"), P("checker-1", self.tenant, "checker"), P("checker-2", self.tenant, "checker")
        self.src = self.source(TEXT.encode("utf-8"), "SYNTHETIC", "t1")
        self.q = "the broker shall not exceed 7 lots"
        self.span = self.span_for(self.src, self.q)

    def source(self, data: bytes, auth: str = "SYNTHETIC", title: str = "doc") -> RegSource:
        s, _ = registry.ingest_document(self.db, self.maker, data, None, {"authority_type": auth, "title": title})
        return s

    def span_for(self, src, quote):
        i = src.text.index(quote)
        return registry.make_span(self.db, self.maker, src, i, i + len(quote), "MANUAL")

    def body(self, key="K1", eff="2026-03-01T00:00:00Z", params=None, condition=None, **kw):
        b = {"rule_key": key, "authority_type": "SYNTHETIC_POLICY", "scope": SCOPE,
             "condition": condition or {"exists": "required_margin_inr"}, "parameters": params or {"mult": {"value": "1.00", "unit": "RATIO"}},
             "effective_from": dt(eff), "span_ids": [self.span.span_id], "rationale": "test rationale",
             "test_vectors": [{"name": "v", "context": CTX | {"required_margin_inr": "1"}, "expect_scope": "TRUE", "expect_condition": "TRUE"}]}
        b.update(kw)
        return b

    def draft(self, **kw):
        return registry.create_rule(self.db, self.maker, self.body(**kw))

    def approved(self, **kw):
        r = self.draft(**kw)
        registry.submit(self.db, self.maker, r.rule_id, "ready", None)
        registry.review(self.db, self.checker, r.rule_id, "APPROVE", "ok", None)
        return r

    def active(self, recorded="2026-01-15T00:00:00Z", as_of="2026-08-01T00:00:00Z", **kw):
        r = self.approved(**kw)
        return registry.activate(self.db, self.checker, r.rule_id, None, "go", as_of=dt(as_of), recorded_at=dt(recorded))

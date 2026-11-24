import pytest
from sqlalchemy import select

from app.core.errors import Blocked, ValidationFailed
from app.engines.regulatory import documents as D
from app.engines.regulatory import registry
from app.engines.regulatory.fixtures import DOCS
from app.engines.regulatory.models import RegRule, RegSpan
from tests.engines.regulatory.helpers import TEXT, fake_services


def _blank_pdf(n_blank=1, text_pages=1) -> bytes:
    fitz = pytest.importorskip("pymupdf")
    doc = fitz.open()
    for i in range(text_pages):
        pg = doc.new_page()
        pg.insert_text((72, 72), f"The member shall not exceed {5 + i} lots on DEMO_NSE.")
    for _ in range(n_blank):
        doc.new_page()
    out = doc.tobytes()
    doc.close()
    return out


def test_r_a01_span_offsets_match_text_and_original_bytes_even_with_multibyte(env):
    sp = env.span
    assert env.src.text[sp.start:sp.end] == sp.quote == env.q
    raw = TEXT.encode("utf-8")
    assert raw[sp.byte_start:sp.byte_end].decode("utf-8") == env.q
    assert sp.byte_start != sp.start  # 'Ü', '✓' make byte offsets differ from character offsets: both are tracked
    assert D.validate_span(env.src, sp) == []
    assert sp.page == 1


def test_span_validation_detects_tampered_text_or_bytes(env):
    env.src.text = env.src.text.replace("broker", "BROKER")
    assert "QUOTE_MISMATCH_TEXT" in D.validate_span(env.src, env.span)
    env.db.rollback()


def test_magic_bytes_and_content_type_checks(env):
    with pytest.raises(ValidationFailed):
        registry.ingest_document(env.db, env.maker, b"%PDF-1.4 fake", "text/plain", {"authority_type": "SYNTHETIC", "title": "x"})
    with pytest.raises(ValidationFailed):  # binary garbage
        registry.ingest_document(env.db, env.maker, b"MZ\x00\x00\x90\x00" * 50, "application/octet-stream", {"authority_type": "SYNTHETIC", "title": "x"})
    with pytest.raises(ValidationFailed):  # invalid utf-8
        registry.ingest_document(env.db, env.maker, b"\xff\xfe\xfa caf\xe9", None, {"authority_type": "SYNTHETIC", "title": "x"})
    with pytest.raises(ValidationFailed):
        registry.ingest_document(env.db, env.maker, b"", None, {"authority_type": "SYNTHETIC", "title": "x"})
    with pytest.raises(ValidationFailed):
        registry.ingest_document(env.db, env.maker, b"hello", None, {"authority_type": "NOPE", "title": "x"})


def test_dedup_by_sha256_and_size_limit(env, monkeypatch):
    a, dup1 = registry.ingest_document(env.db, env.maker, b"same bytes", None, {"authority_type": "SYNTHETIC", "title": "a"})
    b, dup2 = registry.ingest_document(env.db, env.maker, b"same bytes", None, {"authority_type": "SYNTHETIC", "title": "b"})
    assert a.source_id == b.source_id and not dup1 and dup2
    from app.core.config import get_settings
    monkeypatch.setattr(get_settings(), "max_upload_bytes", 10)
    with pytest.raises(ValidationFailed):
        registry.ingest_document(env.db, env.maker, b"x" * 11, None, {"authority_type": "SYNTHETIC", "title": "big"})


def test_html_visible_text_only_no_script(env):
    html = b"<html><head><style>p{}</style></head><body><p>A client shall not exceed 9 lots.</p><script>alert(1)</script></body></html>"
    s = env.source(html, title="h")
    assert s.kind == "html" and "alert" not in s.text and "shall not exceed 9 lots" in s.text


def test_pdf_text_extracted_and_missing_page_blocks_extraction_g14(env):
    good = env.source(_blank_pdf(0, 1), title="pdf-ok")
    assert good.kind == "pdf" and good.unreadable_pages == [] and "shall not exceed" in good.text
    bad = env.source(_blank_pdf(1, 1), title="pdf-gap")
    assert bad.unreadable_pages == [2]
    with pytest.raises(Blocked) as e:
        registry.extract(env.db, env.maker, bad.source_id, "DETERMINISTIC")
    assert e.value.code == "MISSING_SOURCE_PAGE"  # G14: no authoritative decision from an incomplete source


def test_replay_extraction_is_grounded_and_labelled_and_fabricated_quote_is_not(env):
    from app.engines.regulatory.fixtures import D as DD
    name = DOCS[0][0]
    s = env.source((DD.fixtures_dir() / "documents" / name).read_bytes(), title="circ")
    cs = registry.extract(env.db, env.maker, s.source_id, "REPLAY")
    assert len(cs) == 4 and all(c.mode == "REPLAY" and c.confidence_category == "REPLAY_LABELLED" for c in cs)
    ungrounded = [c for c in cs if not c.grounded]
    assert len(ungrounded) == 1 and "QUOTE_NOT_FOUND" in ungrounded[0].issues and ungrounded[0].span_ids == []
    for c in cs:
        for sid in c.span_ids:
            assert D.validate_span(s, env.db.get(RegSpan, sid)) == []


def test_replay_requires_exact_source_bytes(env):
    s = env.source(b"A different document. The member shall hold 1 lots.", title="other")
    with pytest.raises(Blocked) as e:
        registry.extract(env.db, env.maker, s.source_id, "REPLAY")
    assert e.value.code == "NO_REPLAY_FIXTURE"


def test_live_model_is_blocked_with_no_silent_fallback(env):
    with pytest.raises(Blocked) as e:
        registry.extract(env.db, env.maker, env.src.source_id, "LIVE_MODEL")
    assert e.value.code == "LIVE_MODEL_DISABLED"


def test_deterministic_extraction_flags_ambiguity_scope_gaps_and_contradictions(env):
    text = ("1. Rules\n"
            "1.1 The clearing member shall apply a margin multiplier of 1.25 times the base margin on DEMO_NSE.\n"
            "1.2 The clearing member shall apply a margin multiplier of 1.50 times the base margin on DEMO_NSE.\n"
            "1.3 A client shall not exceed 40 on DEMO_NSE, see paragraph 9.9.\n")
    s = env.source(text.encode(), title="det")
    cs = registry.extract(env.db, env.maker, s.source_id, "DETERMINISTIC")
    assert len(cs) == 3
    assert all("CONTRADICTION" in c.issues for c in cs[:2]) and "CONTRADICTION" not in cs[2].issues
    assert "AMBIGUOUS_UNIT" in cs[2].issues and any(i.startswith("UNRESOLVED_REFERENCE:paragraph 9.9") for i in cs[2].issues)
    assert cs[0].fields["comparator"] == "eq" and cs[0].fields["value"] == "1.25" and cs[0].fields["unit"] == "RATIO"
    assert all(c.confidence_category == "PATTERN_MATCH" for c in cs)


def test_r_a08_injection_text_is_data_never_an_instruction(env):
    from app.engines.regulatory.fixtures import D as DD
    data = (DD.fixtures_dir() / "documents" / "DEMO-NEGATIVE-INJECTION.txt").read_bytes()
    s = env.source(data, title="inj")
    assert s.risk_flags  # flagged for the reviewer
    called = []
    with fake_services(**{"risk.compute_margin": lambda *a, **k: called.append(a), "collateral.coverage": lambda *a, **k: called.append(a),
                          "compliance.evaluate": lambda *a, **k: called.append(a)}):
        cs = registry.extract(env.db, env.maker, s.source_id, "DETERMINISTIC")
    assert called == []  # no tool escalation
    assert env.db.execute(select(RegRule).where(RegRule.tenant_id == env.tenant)).first() is None  # nothing proposed as a rule, let alone activated
    assert [c.fields["value"] for c in cs] == ["50"]  # only the legitimate obligation was proposed
    assert all("SOURCE_HAS_INJECTION_PATTERNS" in c.issues for c in cs)
    assert not any("approve_rule" in str(c.fields) for c in cs)  # injected text never became a field
    from app.engines.evidence.models import EvEvent
    actions = set(env.db.execute(select(EvEvent.action).where(EvEvent.tenant_id == env.tenant)).scalars())
    assert actions <= {"SOURCE_INGESTED", "CANDIDATES_EXTRACTED"}  # no rule/approval/activation evidence from document text


def test_fixture_sources_parse(env):
    for name, auth, *_ in DOCS:
        from app.engines.regulatory.fixtures import D as DD
        s = env.source((DD.fixtures_dir() / "documents" / name).read_bytes(), auth, name)
        assert s.page_count == 1 and s.text

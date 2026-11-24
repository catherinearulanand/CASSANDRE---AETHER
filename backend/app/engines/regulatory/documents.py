"""R01/R02: safe ingestion, text extraction, span grounding, candidate extraction.

Document text is UNTRUSTED DATA. Nothing in this module (or its callers) ever treats document text as an
instruction: extraction only produces proposals; tools/rules are never invoked from text.
"""
from __future__ import annotations

import io
import json
import re
from datetime import date
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path

from app.core.canonical import sha256_hex
from app.core.config import REPO_ROOT, get_settings
from app.core.errors import ValidationFailed
from app.core.money import NumericError, to_decimal

PARSER_VERSION = "aether-reg-parser/1.0"
MAX_PAGES = 200
MAX_TEXT_CHARS = 5_000_000
INJECTION_PATTERNS = [
    r"ignore (all |any )?(previous|prior|above) (instructions|rules)", r"disregard (the )?(system|previous)",
    r"you are now", r"system prompt", r"activate (this |the )?rule", r"call (the )?tool", r"execute (the )?(following|command)",
    r"approve (this|the) rule", r"override (the )?(policy|approval)",
]
_INJ = [re.compile(p, re.I) for p in INJECTION_PATTERNS]
CT = {"pdf": "application/pdf", "html": "text/html", "text": "text/plain"}


class _TextHTML(HTMLParser):
    """Visible-text extractor. convert_charrefs only; no external entity/DTD resolution of any kind."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.skip += 1
        if tag in ("p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "table"):
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def sniff(data: bytes, declared_content_type: str | None) -> str:
    head = data[:1024].lstrip(b"\xef\xbb\xbf \t\r\n")
    if data.startswith(b"%PDF-"):
        kind = "pdf"
    elif head[:1] == b"<" and (b"<html" in head.lower() or b"<!doctype html" in head.lower()):
        kind = "html"
    elif b"\x00" in data[:8192]:
        raise ValidationFailed("Unsupported or binary content (magic bytes do not match PDF/HTML/text)")
    else:
        kind = "text"
    if declared_content_type:
        ct = declared_content_type.split(";")[0].strip().lower()
        if ct not in ("application/octet-stream", "") and ct != CT[kind]:
            raise ValidationFailed(f"Declared content type {ct} disagrees with detected {kind} content ({CT[kind]})",
                                   field_errors={"file": "content-type/magic-byte mismatch"})
    return kind


def extract_text(data: bytes, kind: str) -> dict:
    if kind == "pdf":
        from pypdf import PdfReader
        try:
            reader = PdfReader(io.BytesIO(data), strict=False)
            if reader.is_encrypted:
                raise ValidationFailed("Encrypted PDFs are not accepted")
            n = len(reader.pages)
        except ValidationFailed:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ValidationFailed(f"Unreadable PDF: {type(exc).__name__}") from exc
        if n > MAX_PAGES or n == 0:
            raise ValidationFailed(f"PDF page count {n} outside 1..{MAX_PAGES}")
        pages, bad = [], []
        for i, p in enumerate(reader.pages, start=1):
            try:
                t = (p.extract_text() or "").replace("\f", " ")
            except Exception:  # noqa: BLE001
                t = ""
            if not t.strip():
                bad.append(i)  # never silently infer missing content (OCR is an optional, unconfigured adapter)
            pages.append(t)
        return {"text": "\f".join(pages), "pages": n, "unreadable": bad}
    try:
        txt = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValidationFailed("Text must be valid UTF-8") from exc
    if kind == "html":
        p = _TextHTML()
        p.feed(txt)
        txt = re.sub(r"[ \t]+\n", "\n", re.sub(r"\n{3,}", "\n\n", "".join(p.out))).strip()
    if len(txt) > MAX_TEXT_CHARS:
        raise ValidationFailed("Document text too large")
    return {"text": txt, "pages": txt.count("\f") + 1, "unreadable": []}


def injection_flags(text: str) -> list[str]:
    return sorted({p.pattern for p in _INJ if p.search(text)})


def page_of(text: str, offset: int) -> int:
    return text.count("\f", 0, offset) + 1


def byte_offsets(kind: str, raw: bytes, text: str, start: int, end: int) -> tuple[int | None, int | None]:
    """UTF-8 byte offsets, only meaningful when the stored text IS the decoded original (plain-text docs)."""
    if kind != "text":
        return None, None
    base = 3 if raw.startswith(b"\xef\xbb\xbf") else 0
    return base + len(text[:start].encode("utf-8")), base + len(text[:end].encode("utf-8"))


def validate_span(source, span) -> list[str]:
    """R-A01: the quote must equal the stored text AND (for text documents) the original bytes."""
    problems = []
    if not (0 <= span.start < span.end <= len(source.text)):
        return ["OFFSETS_OUT_OF_RANGE"]
    if source.text[span.start:span.end] != span.quote:
        problems.append("QUOTE_MISMATCH_TEXT")
    if source.kind == "text" and span.byte_start is not None:
        if bytes(source.raw_bytes)[span.byte_start:span.byte_end].decode("utf-8", "replace") != span.quote:
            problems.append("QUOTE_MISMATCH_BYTES")
    if sha256_hex(bytes(source.raw_bytes)) != source.sha256:
        problems.append("SOURCE_BYTES_HASH_MISMATCH")
    return problems


# ---------------------------------------------------------------- extraction
_OBLIG = re.compile(
    r"\b(?P<actor>(?:the|a|an)\s[a-z][a-z ]*?)\sshall\s(?P<action>[^.;]*?)\b(?P<cmp>not exceed|at least|of)\s+"
    r"(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?P<unit>times|per cent|lots|%)?", re.I)
_DATE = re.compile(r"With effect from (?P<d>\d{1,2} [A-Z][a-z]+ \d{4})", re.I)
_VENUE = re.compile(r"\b(DEMO_[A-Z]+)\b")
_REF = re.compile(r"(?:paragraph\s+(\d+(?:\.\d+)*)|(Annexure\s+[A-Z]))", re.I)
UNIT_MAP = {"times": "RATIO", "lots": "LOTS", "per cent": "PERCENT", "%": "PERCENT"}
CMP_MAP = {"not exceed": "lte", "at least": "gte", "of": "eq"}
_MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                        "september", "october", "november", "december"], 1)}


def _sentences(text: str):
    for m in re.finditer(r"[^.\n]+(?:\.\d+[^.\n]*)*\.?", text):
        s = m.group(0)
        if s.strip():
            yield m.start(), m.end(), s


def _parse_date(s: str) -> str | None:
    p = s.split()
    try:
        return date(int(p[2]), _MONTHS[p[1].lower()], int(p[0])).isoformat()
    except (KeyError, ValueError, IndexError):
        return None


def _ref_issues(text: str, quote: str) -> list[str]:
    out = []
    for m in _REF.finditer(quote):
        if m.group(1):
            if not re.search(rf"^\s*{re.escape(m.group(1))}\s", text, re.M):
                out.append(f"UNRESOLVED_REFERENCE:paragraph {m.group(1)}")
        elif not re.search(rf"^\s*{re.escape(m.group(2))}\s*$", text, re.M | re.I):
            out.append(f"UNRESOLVED_REFERENCE:{m.group(2)}")
    return out


def deterministic_candidates(text: str) -> list[dict]:
    """Pattern-based obligation finder. Anything it cannot read unambiguously is flagged, never guessed."""
    out = []
    for s0, _e0, sent in _sentences(text):
        m = _OBLIG.search(sent)
        if not m:
            continue
        # quote = the sentence without its leading paragraph number
        lead = re.match(r"\s*(?:\d+(?:\.\d+)*|[A-Z]\.\d+)\s+", sent)
        q0 = s0 + (lead.end() if lead else 0)
        quote = text[q0:s0 + len(sent)].strip()
        q0 = text.index(quote, q0)
        unit_raw = (m.group("unit") or "").lower() or None
        issues = []
        if unit_raw is None:
            issues.append("AMBIGUOUS_UNIT")
        try:
            value = str(to_decimal(m.group("num").replace(",", "")))
        except NumericError:
            value, issues = None, issues + ["UNREADABLE_VALUE"]
        dm, vm = _DATE.search(sent), _VENUE.search(sent)
        scope = {"venue": vm.group(1)} if vm else {}
        if not scope:
            issues.append("SCOPE_GAP")
        issues += _ref_issues(text, quote)
        metric = re.sub(r"^(apply|hold|maintain|keep)\s+(a|an|the)?\s*", "", m.group("action").strip(), flags=re.I) or m.group("action").strip()
        out.append({"quote": quote, "start": q0, "fields": {
            "obligated_actor": m.group("actor").strip(), "action": m.group("action").strip(), "metric": metric,
            "comparator": CMP_MAP[m.group("cmp").lower()], "value": value, "unit_as_written": unit_raw,
            "unit": UNIT_MAP.get(unit_raw), "scope": scope, "exceptions": [],
            "deadline": _parse_date(dm.group("d")) if dm else None, "dependencies": []}, "issues": issues})
    return out


def replay_fixture_for(sha: str) -> dict | None:
    """REPLAY mode = hand-labelled fixture keyed by the SHA-256 of the exact source bytes. Not a model run."""
    d = REPO_ROOT / "fixtures" / "regulatory" / "replay"
    for f in sorted(d.glob("*.replay.json")) if d.exists() else []:
        j = json.loads(f.read_text(encoding="utf-8"))
        src = REPO_ROOT / "fixtures" / "regulatory" / "documents" / j["source_file"]
        if src.exists() and sha256_hex(src.read_bytes()) == sha:
            return j
    return None


def locate_quote(text: str, quote: str) -> tuple[int | None, list[str]]:
    """Literal-quote grounding: exactly one exact occurrence, else an explicit issue (no fuzzy matching)."""
    n = text.count(quote) if quote else 0
    if n == 0:
        return None, ["QUOTE_NOT_FOUND"]
    if n > 1:
        return None, ["AMBIGUOUS_QUOTE"]
    return text.index(quote), []


def mark_contradictions(cands: list[dict]) -> None:
    by: dict[tuple, list[dict]] = {}
    for c in cands:
        f = c["fields"]
        by.setdefault((f.get("metric"), json.dumps(f.get("scope"), sort_keys=True), f.get("comparator"), f.get("deadline")), []).append(c)
    for grp in by.values():
        vals = {(c["fields"].get("value"), c["fields"].get("unit")) for c in grp}
        if len(grp) > 1 and len(vals) > 1:
            for c in grp:
                c["issues"].append("CONTRADICTION")


def validate_manual_candidate(raw: dict) -> dict:
    if not isinstance(raw, dict) or not raw.get("quote") or not isinstance(raw.get("fields"), dict):
        raise ValidationFailed("manual candidates need {quote, fields}")
    return raw


def settings_live_enabled() -> bool:
    return get_settings().live_model_enabled


def numeric_str(v) -> str | None:
    try:
        return str(Decimal(str(v)))
    except Exception:  # noqa: BLE001
        return None


def fixtures_dir() -> Path:
    return REPO_ROOT / "fixtures" / "regulatory"

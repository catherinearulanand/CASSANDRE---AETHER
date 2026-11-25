"""COVENANT extraction (pure; no DB).

Two modes, ALWAYS labelled on every proposal:
  RULES  - deterministic clause/table extractor (regular expressions over structured fictional clauses).
  REPLAY - hand-labelled proposals loaded from fixtures/contracts/replay/*.json (bound to the document by SHA-256).
LIVE_MODEL is not implemented in this build (hosted provider disabled by default) and is refused explicitly.
Document text is untrusted DATA: it is only ever matched against fixed patterns, never interpreted as instructions.
Every proposal is re-validated here: quote offsets must reproduce the quote byte-for-byte, direction must agree
with the party named in the quote, values must pass terms.validate_value. Failures become `issues` (field is
UNRESOLVED, not silently corrected). Confidence scores are deliberately NOT produced.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.core.config import REPO_ROOT
from app.engines.contracts import terms as T

REPLAY_DIR = REPO_ROOT / "fixtures" / "contracts" / "replay"
HEADER_DATE = re.compile(r"Effective Date: (\d{4}-\d{2}-\d{2})\.")
HEADER_PARTIES = re.compile(r"Party A: (.+?)\. Party B: (.+?)\.")
_AMT = r"[A-Z]{3} [0-9.,]+"
RE_MONEY = re.compile(r"(?P<label>Threshold|Minimum Transfer Amount|Independent Amount) with respect to "
                      r"(?P<party>[^:\n]+): (?P<amt>" + _AMT + r")\.")
RE_COND = re.compile(r"If the Credit Rating of (?P<rp>[^,]+?) is below (?P<rt>[A-D][A-Za-z+\-]*) on the relevant "
                     r"Valuation Date, the (?P<label>Threshold|Minimum Transfer Amount|Independent Amount) with "
                     r"respect to (?P<party>.+?) is (?P<amt>" + _AMT + r")\.")
RE_ROUND = re.compile(r"rounded (?P<mode>up|down|half up|half even) to the nearest integral multiple of (?P<amt>" + _AMT + r")\.")
RE_MTA_SEM = re.compile(r"only if the unrounded amount is (?P<cmp>greater than or equal to|greater than) the Minimum Transfer Amount\.")
RE_NET = re.compile(r"Close-out netting applies across all Transactions[^.]*?netting set (?P<ns>[A-Z0-9\-]+)\.")
RE_SEG = re.compile(r"Initial Margin, if any, is (?P<neg>not )?held in segregated accounts\.")
RE_VAL_AGENT = re.compile(r"The Valuation Agent is (?P<p>[^.]+)\.")
RE_CALL = re.compile(r"notice no later than (?P<t>\d{2}:\d{2}) ")
RE_WRT = re.compile(r"(?:Threshold|Minimum Transfer Amount|Independent Amount) with respect to (.+?)(?::| is )")
RE_COMMENT = re.compile(r"<!--.*?-->", re.S)
RE_NET_NEG = re.compile(r"Close-out netting does not apply to [^.]*\.")
# Passages that say they are NOT operative (illustrations, drafts, hidden text). A clause-shaped sentence inside such a
# block must never look approve-ready: it is downgraded to UNRESOLVED for a human to confirm.
RE_NON_OPERATIVE = re.compile(
    r"not operative|non-operative|explanatory only|by way of example|for illustration|illustration|example only|"
    r"for explanation only|hidden text|white on white|not part of the agreement|superseded|deleted|struck|"
    r"for information only|draft only|has been replaced", re.I)
# Instruction-shaped text addressed to automated systems (prompt injection). It is DATA: surfaced, never executed.
RE_INJECTION = re.compile(
    r"ignore (?:all |any )?(?:previous|prior|above) instructions|automated systems?|\bsystem\s*:|you are now|maintenance mode|"
    r"approve (?:every|all)\b|call the \w+ tool|\bexecute\s*:|\bfreeze\s*\(|export the audit|disregard (?:the )?(?:above|previous)", re.I)
# Sentences that talk about material terms but matched no deterministic pattern: surfaced, never silently dropped.
RE_MATERIAL_WORDS = re.compile(
    r"\b(?:Threshold|Minimum Transfer Amount|Independent Amount|Credit Rating|netting|Valuation Agent|Haircut|"
    r"Valuation Percentage|rounded|rounding|segregated|Eligible Collateral|Concentration Cap)\b", re.I)
RE_HEADING = re.compile(r"^(?:Paragraph \d+\.\s*)?[A-Z][A-Za-z\- ]{0,60}\.?$")
LABEL_KEY = {"Threshold": "threshold", "Minimum Transfer Amount": "mta", "Independent Amount": "independent_amount"}
COLS = ["Asset", "Currency", "Issuer", "Valuation Percentage", "Haircut", "FX Haircut", "Concentration Cap",
        "Min Rating", "Max Maturity"]


class ExtractionError(ValueError):
    def __init__(self, code: str, msg: str):
        super().__init__(msg)
        self.code = code


def parse_money(s: str) -> tuple[dict | None, str | None]:
    m = re.fullmatch(r"([A-Z]{3}) ([0-9][0-9.,]*)", s.strip())
    if not m:
        return None, f"INVALID_AMOUNT_FORMAT: {s!r}"
    ccy, a = m.groups()
    if re.fullmatch(r"\d{1,3}(\.\d{3})+(,\d+)?", a) or re.fullmatch(r"\d+,\d{1,2}", a):
        return None, f"AMBIGUOUS_DECIMAL_SEPARATOR: {a!r} (European vs US grouping); review required"
    if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?", a):
        a = a.replace(",", "")
    elif not re.fullmatch(r"\d+(\.\d+)?", a):
        return None, f"INVALID_AMOUNT_FORMAT: {a!r}"
    return {"amount": a, "currency": ccy}, None


def header_info(text: str) -> dict:
    d, p = HEADER_DATE.search(text), HEADER_PARTIES.search(text)
    return {"effective_date": d.group(1) if d else None, "party_a": p.group(1) if p else None,
            "party_b": p.group(2) if p else None}


def _prop(key, direction, value, m, off=0, condition=None, issues=None, unit=None):
    return {"field_key": key, "direction": direction, "value": value, "quote": m.group(0),
            "start": m.start() + off, "end": m.end() + off, "condition": condition, "issues": list(issues or []),
            "unit": unit}


_ALIASES = {"Party A": "A", "Party B": "B"}  # the header defines Party A/B, and intake checks it against the package


def _side(party: str, names: dict[str, str]) -> tuple[str | None, list[str]]:
    p = party.strip()
    if p in _ALIASES:
        return _ALIASES[p], []
    if p in names:
        return names[p], []
    return None, [f"UNKNOWN_PARTY: {p!r} is neither Party A nor Party B of this package"]


def _parse_table(text: str) -> dict | None:
    lines, pos, blk = text.splitlines(keepends=True), 0, None
    rows, start, end, hdr = [], None, None, None
    for ln in lines:
        s = ln.rstrip("\n").rstrip("\r")
        if hdr is None and s.startswith("|") and "Valuation Percentage" in s:
            hdr, start, end = [c.strip() for c in s.strip("|").split("|")], pos, pos + len(s)
        elif hdr is not None and s.startswith("|"):
            rows.append([c.strip() for c in s.strip("|").split("|")])
            end = pos + len(s)
        elif hdr is not None:
            break
        pos += len(ln)
    if hdr is None:
        return None
    return {"hdr": hdr, "rows": rows, "start": start, "end": end}


def _pct_cell(c: str) -> str | None:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)%", c)
    return m.group(1) if m else None


def _schedule(text: str) -> dict | None:
    tb = _parse_table(text)
    if tb is None:
        return None
    issues: list[str] = []
    # Columns are located BY HEADER NAME (any order). A missing/extra/duplicated column is flagged, never guessed.
    if sorted(tb["hdr"]) != sorted(COLS):
        return {"field_key": "eligible_schedule", "direction": None, "value": None,
                "quote": text[tb["start"]:tb["end"]], "start": tb["start"], "end": tb["end"], "condition": None,
                "issues": [f"SCHEDULE_COLUMNS_UNEXPECTED: {tb['hdr']}"], "unit": None}
    pos = {name: tb["hdr"].index(name) for name in COLS}
    out = []
    for i, r in enumerate(tb["rows"], 1):
        if len(r) != len(COLS):
            issues.append(f"row{i}: expected {len(COLS)} cells, found {len(r)}")
            continue
        asset, ccy, iss, val, hc, fx, cap, mr, mm = (r[pos[c]] for c in COLS)
        row: dict[str, Any] = {"asset_class": asset, "currency": ccy, "issuer_class": iss}
        for k, c in (("valuation_pct", val), ("haircut_pct", hc), ("fx_haircut_pct", fx)):
            v = _pct_cell(c)
            if v is None:
                issues.append(f"row{i}: {k} cell {c!r} is not a percentage")
            row[k] = v
        cm = re.fullmatch(r"(\d+(?:\.\d+)?)% of (obligation|total allocated)", cap)
        if cap == "none":
            row["concentration_cap_pct"], row["cap_denominator"] = None, None
        elif cm:
            row["concentration_cap_pct"] = cm.group(1)
            row["cap_denominator"] = "OBLIGATION" if cm.group(2) == "obligation" else "TOTAL_EFFECTIVE_ALLOCATED"
        else:
            issues.append(f"row{i}: concentration cap {cap!r} not understood")
        row["min_rating"] = None if mr == "none" else mr
        row["max_maturity_years"] = None if mm == "none" else mm
        out.append(row)
    return {"field_key": "eligible_schedule", "direction": None, "value": out if not issues else None,
            "quote": text[tb["start"]:tb["end"]], "start": tb["start"], "end": tb["end"], "condition": None,
            "issues": issues, "unit": None}


def extract_rules(text: str, party_a: dict, party_b: dict, base_ccy: str) -> list[dict]:
    """party_x = {"id","name"}. Returns proposals (unvalidated beyond parsing; run check_proposal after)."""
    names = {party_a["name"]: "A", party_b["name"]: "B"}
    ids = {"A": party_a["id"], "B": party_b["id"]}
    props: list[dict] = []
    for m in RE_COND.finditer(text):
        side, iss = _side(m.group("party"), names)
        rside, riss = _side(m.group("rp"), names)
        val, err = parse_money(m.group("amt"))
        iss += riss + ([err] if err else [])
        cond = {"fact": f"rating:{ids[rside]}", "op": "lt", "value": m.group("rt"), "scale": "RATING"} if rside else None
        key = LABEL_KEY[m.group("label")] + (f"_{side.lower()}" if side else "")
        props.append(_prop(key, side, val, m, condition=cond, issues=iss))
    for m in RE_MONEY.finditer(text):
        side, iss = _side(m.group("party"), names)
        val, err = parse_money(m.group("amt"))
        if err:
            iss.append(err)
        props.append(_prop(LABEL_KEY[m.group("label")] + (f"_{side.lower()}" if side else ""), side, val, m, issues=iss))
    for m in RE_ROUND.finditer(text):
        val, err = parse_money(m.group("amt"))
        props.append(_prop("rounding_mode", None, m.group("mode").upper().replace(" ", "_"), m))
        props.append(_prop("rounding_increment", None, val, m, issues=[err] if err else []))
    for m in RE_MTA_SEM.finditer(text):
        props.append(_prop("mta_semantics", None, "GTE" if m.group("cmp") == "greater than or equal to" else "GT", m))
    for m in RE_NET.finditer(text):
        props.append(_prop("netting_authorization", None, True, m, issues=[], unit=m.group("ns")))
    for m in RE_NET_NEG.finditer(text):
        props.append(_prop("netting_authorization", None, False, m, issues=[]))
    for m in RE_SEG.finditer(text):
        props.append(_prop("segregated_im", None, m.group("neg") is None, m))
    for m in RE_VAL_AGENT.finditer(text):
        side, iss = _side(m.group("p"), names)
        props.append(_prop("valuation_agent", None, side, m, issues=iss))
    for m in RE_CALL.finditer(text):
        props.append(_prop("call_deadline", None, m.group("t"), m))
    sch = _schedule(text)
    if sch:
        props.append(sch)
    comments = [(m.start(), m.end()) for m in RE_COMMENT.finditer(text)]
    for pr in props:  # clause-shaped text inside an explicitly non-operative block must not look approve-ready
        if pr["field_key"] == "unparsed_clause":
            continue
        if RE_NON_OPERATIVE.search(_block(text, pr["start"], pr["end"])):
            pr["issues"] = list(pr.get("issues") or []) + [
                "NON_OPERATIVE_CONTEXT: clause sits in a passage marked illustrative/hidden/non-operative; confirm it is operative"]
        elif any(a < pr["end"] and pr["start"] < b for a, b in comments):
            pr["issues"] = list(pr.get("issues") or []) + [
                "NON_OPERATIVE_CONTEXT: clause sits inside a comment (<!-- ... -->), which is not contract text; confirm it is operative"]
    return props + _residual(text, props)


def _block(text: str, s: int, e: int) -> str:
    """The blank-line-delimited paragraph block containing [s, e)."""
    a = text.rfind("\n\n", 0, s)
    b = text.find("\n\n", e)
    return text[(0 if a < 0 else a + 2):(len(text) if b < 0 else b)]


def _residual(text: str, props: list[dict]) -> list[dict]:
    """Surface material-looking sentences no deterministic pattern covered (paraphrases, unit words, unsupported rating
    scales, negative statements) and instruction-shaped text. Each becomes an UNRESOLVED `unparsed_clause` item: a human must
    read and REJECT/transcribe it before freeze, so nothing material disappears silently. Headings are skipped."""
    spans = [(p["start"], p["end"]) for p in props]
    out: list[dict] = []
    pos = 0
    for line in text.split("\n"):
        ls, le = pos, pos + len(line)
        pos = le + 1
        if not line.strip():
            continue  # (table rows are NOT skipped: rows of the parsed eligibility table are covered by its span; any other table is surfaced)
        for m in re.finditer(r"[^.;]+(?:\.[0-9][^.;]*)*[.;]?", line):  # sentence-ish pieces; keeps decimals like 1,250,000.50 intact
            seg = m.group(0)
            ss, se = ls + m.start(), ls + m.end()
            body = seg.strip()
            if len(body.split()) < 4 or RE_HEADING.match(body):
                continue
            if any(a < se and ss < b for a, b in spans):
                continue
            inj = RE_INJECTION.search(body)
            if inj:
                out.append({"field_key": "unparsed_clause", "direction": None, "value": body[:300], "quote": text[ss:se],
                            "start": ss, "end": se, "condition": None, "unit": None,
                            "issues": ["SUSPECTED_PROMPT_INJECTION: instruction-shaped text addressed to automated systems; "
                                       "treated as data, never executed; reviewer should reject it"]})
            elif RE_MATERIAL_WORDS.search(body):
                out.append({"field_key": "unparsed_clause", "direction": None, "value": body[:300], "quote": text[ss:se],
                            "start": ss, "end": se, "condition": None, "unit": None,
                            "issues": ["UNPARSED_MATERIAL_CLAUSE: wording about material terms matched no deterministic pattern "
                                       "(paraphrase, spelled-out amount, unsupported rating scale or negation); read it and key it in or reject"]})
    return out


def check_proposal(p: dict, text: str, party_a_name: str, party_b_name: str, base_ccy: str,
                   package_netting_set: str | None = None) -> tuple[dict, list[str]]:
    """Validate a proposal from ANY mode. Returns (normalised proposal, issues). Non-empty issues => UNRESOLVED."""
    issues = list(p.get("issues") or [])
    q, s, e = p.get("quote"), p.get("start"), p.get("end")
    raw = text.encode("utf-8")
    if not q or not isinstance(s, int) or not isinstance(e, int) or not (0 <= s < e <= len(text)) or text[s:e] != q:
        issues.append("QUOTE_OFFSET_MISMATCH: quote does not reproduce from the stored document at the cited offsets")
    elif len(raw) != len(text):
        # non-ASCII document: offsets are Unicode character offsets; recorded for transparency
        issues.append("NON_ASCII_DOCUMENT_REVIEW: offsets are character offsets; byte offsets differ")
    key = p.get("field_key")
    if key == "unparsed_clause":  # pseudo-item: carries its own issues; never has a validated value
        return p, issues
    spec = T.FIELD_CATALOG.get(key)
    if spec is None:
        return p, issues + [f"UNKNOWN_FIELD_KEY: {key}"]
    names = {party_a_name: "A", party_b_name: "B", **_ALIASES}
    if spec["direction"] and p.get("direction") != spec["direction"]:
        issues.append(f"DIRECTION_MISMATCH: {key} requires direction {spec['direction']}, proposal says {p.get('direction')}")
    wrt = RE_WRT.search(q or "")
    if spec["direction"] and wrt and names.get(wrt.group(1).strip()) != spec["direction"]:
        issues.append(f"DIRECTION_MISMATCH: quote is stated with respect to {wrt.group(1).strip()!r} which is Party "
                      f"{names.get(wrt.group(1).strip(), '?')}, not Party {spec['direction']} (swapped parties?)")
    if key == "valuation_agent" and p.get("value") in ("A", "B"):
        who = party_a_name if p["value"] == "A" else party_b_name
        if who not in (q or ""):
            issues.append("DIRECTION_MISMATCH: valuation agent value disagrees with the quoted party")
    cond = p.get("condition")
    if cond is not None:
        if key not in T.CONDITIONAL_OK:
            issues.append(f"CONDITION_NOT_ALLOWED_FOR_FIELD:{key}")
        issues += [f"CONDITION_INVALID: {x}" for x in T.validate_predicate(cond)]
    if key == "netting_authorization" and package_netting_set and p.get("unit") not in (None, package_netting_set):
        issues.append(f"NETTING_SET_MISMATCH: clause names {p.get('unit')!r}, package is {package_netting_set!r}")
    val = p.get("value")
    if val is None:
        if not issues:
            issues.append("VALUE_MISSING")
        return p, issues
    errs, canon = T.validate_value(key, val, base_ccy)
    issues += [f"VALIDATION: {x}" for x in errs]
    out = dict(p)
    out["value"] = canon if not errs else val
    return out, issues


# ----------------------------------------------------------------------------- replay
def load_replay(doc_ref: str, sha256: str) -> list[dict]:
    """Hand-labelled proposals bound to the document bytes. Same doc_ref but different bytes => source mismatch."""
    if not REPLAY_DIR.exists():
        raise ExtractionError("NO_REPLAY_FOR_DOCUMENT", "replay fixture directory missing")
    for f in sorted(REPLAY_DIR.glob("*.replay.json")):
        j = json.loads(f.read_text(encoding="utf-8"))
        if j["doc_ref"] != doc_ref:
            continue
        if j["doc_sha256"] != sha256:
            raise ExtractionError("REPLAY_SOURCE_MISMATCH",
                                  f"replay labels for {doc_ref} were made against different document bytes")
        return j["proposals"]
    raise ExtractionError("NO_REPLAY_FOR_DOCUMENT", f"no hand-labelled replay fixture for {doc_ref}")

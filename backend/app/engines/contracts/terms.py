"""COVENANT pure domain logic (no DB): field catalogue, validation, typed conditional predicates,
hierarchy-aware effective-dated resolution.

Conventions (documented in docs/methodologies/contracts.md):
  * Direction 'A'/'B' = Party A / Party B as named in the package. threshold_a is the Threshold stated
    "with respect to Party A" in the document. Direction is never inferred from position in a table.
  * Percentages are PERCENT numbers as decimal strings ("94" = 94 %). valuation_pct and haircut_pct are
    two views of ONE discount: valuation_pct + haircut_pct must equal 100 (never apply both).
  * Money values: {"amount": "<decimal string>", "currency": "<ISO>"}; amounts non-negative where required.
  * Predicates are three-valued (True / False / None=UNKNOWN). UNKNOWN blocks, never passes.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.core.money import SUPPORTED_CURRENCIES, NumericError, dstr, to_decimal

RATING_SCALE = ["AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-", "BB+", "BB", "BB-",
                "B+", "B", "B-", "CCC+", "CCC", "CCC-", "CC", "C", "D"]
ROUNDING_MODES = {"UP", "DOWN", "HALF_UP", "HALF_EVEN"}
MTA_SEMANTICS = {"GTE", "GT"}
CAP_DENOMINATORS = {"OBLIGATION", "TOTAL_EFFECTIVE_ALLOCATED"}
DOC_KINDS = {"MASTER", "SCHEDULE", "CSA", "CONFIRMATION", "AMENDMENT"}
# Reviewed hierarchy semantics: higher rank wins when both entries are effective; among equal rank the
# later effective_from wins. Not "last document wins".
PRECEDENCE_RANK = {"MASTER": 1, "SCHEDULE": 2, "CSA": 3, "CONFIRMATION": 4, "AMENDMENT": 5}

# key -> spec. kind: money|enum|bool|str|list. required: needed to freeze. material: affects amounts.
FIELD_CATALOG: dict[str, dict] = {
    "threshold_a": dict(kind="money", direction="A", required=True, material=True, nonneg=True),
    "threshold_b": dict(kind="money", direction="B", required=True, material=True, nonneg=True),
    "mta_a": dict(kind="money", direction="A", required=True, material=True, nonneg=True),
    "mta_b": dict(kind="money", direction="B", required=True, material=True, nonneg=True),
    "independent_amount_a": dict(kind="money", direction="A", required=True, material=True, nonneg=True),
    "independent_amount_b": dict(kind="money", direction="B", required=True, material=True, nonneg=True),
    "rounding_increment": dict(kind="money", direction=None, required=True, material=True, positive=True),
    "rounding_mode": dict(kind="enum", values=ROUNDING_MODES, direction=None, required=True, material=True),
    "mta_semantics": dict(kind="enum", values=MTA_SEMANTICS, direction=None, required=True, material=True),
    "netting_authorization": dict(kind="bool", direction=None, required=True, material=True),
    "segregated_im": dict(kind="bool", direction=None, required=True, material=True),
    "eligible_schedule": dict(kind="list", direction=None, required=True, material=True),
    "valuation_agent": dict(kind="enum", values={"A", "B"}, direction=None, required=False, material=False),
    "call_deadline": dict(kind="str", direction=None, required=False, material=False),
}
REQUIRED_KEYS = sorted(k for k, v in FIELD_CATALOG.items() if v["required"])
# Keys that may carry a conditional (typed predicate) alternative value.
CONDITIONAL_OK = {"threshold_a", "threshold_b", "mta_a", "mta_b", "independent_amount_a", "independent_amount_b"}


def parse_dt(s: str | datetime) -> datetime:
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=UTC)
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=UTC)


# --------------------------------------------------------------------------------- validation
def _pct(v: Any, name: str, errs: list[str], lo_open: bool = False) -> Decimal | None:
    try:
        d = to_decimal(v, field=name)
    except NumericError as e:
        errs.append(str(e))
        return None
    if d < 0 or d > 100 or (lo_open and d == 0):
        errs.append(f"{name}: {dstr(d)} outside {'(0' if lo_open else '[0'},100]")
        return None
    return d


def validate_schedule(rows: Any, base_ccy: str) -> tuple[list[str], list[dict]]:
    """Returns (errors, normalised rows). Whole schedule rejected if any row is invalid."""
    errs: list[str] = []
    out: list[dict] = []
    if not isinstance(rows, list) or not rows:
        return ["ELIGIBLE_SCHEDULE_EMPTY: at least one eligible asset row is required"], []
    seen = set()
    for i, r in enumerate(rows):
        p = f"row{i + 1}"
        if not isinstance(r, dict):
            errs.append(f"{p}: not an object")
            continue
        ac = str(r.get("asset_class", "")).strip().upper()
        ccy = r.get("currency")
        iss = str(r.get("issuer_class", "")).strip().upper()
        if len(ac) > 40:
            errs.append(f"{p}: asset_class longer than 40 characters")
        if len(iss) > 40:
            errs.append(f"{p}: issuer_class longer than 40 characters")
        if not ac:
            errs.append(f"{p}: asset_class missing")
        if ccy not in SUPPORTED_CURRENCIES:
            errs.append(f"{p}: unsupported currency {ccy!r}")
        if not iss:
            errs.append(f"{p}: issuer_class missing")
        n0 = len(errs)
        val = _pct(r.get("valuation_pct"), f"{p}.valuation_pct", errs, lo_open=True)
        hc = _pct(r.get("haircut_pct"), f"{p}.haircut_pct", errs)
        fx = _pct(r.get("fx_haircut_pct", "0"), f"{p}.fx_haircut_pct", errs)
        if val is not None and hc is not None and val + hc != 100:
            errs.append(f"{p}: VALUATION_HAIRCUT_INCONSISTENT valuation {dstr(val)}% + haircut {dstr(hc)}% != 100% "
                        "(they are the same discount; never apply both)")
        cap, den = r.get("concentration_cap_pct"), r.get("cap_denominator")
        cap_d = None
        if cap is not None:
            cap_d = _pct(cap, f"{p}.concentration_cap_pct", errs, lo_open=True)
            if den not in CAP_DENOMINATORS:
                errs.append(f"{p}: cap_denominator must be one of {sorted(CAP_DENOMINATORS)} when a cap is set")
        elif den is not None:
            errs.append(f"{p}: cap_denominator given without a cap")
        mr = r.get("min_rating")
        if mr is not None and mr not in RATING_SCALE:
            errs.append(f"{p}: unknown min_rating {mr!r}")
        mm = r.get("max_maturity_years")
        mm_d = None
        if mm is not None:
            try:
                mm_d = to_decimal(mm, field=f"{p}.max_maturity_years")
                if mm_d <= 0:
                    errs.append(f"{p}: max_maturity_years must be positive")
            except NumericError as e:
                errs.append(str(e))
        for nm, v in (("valuation_pct", val), ("haircut_pct", hc), ("fx_haircut_pct", fx), ("concentration_cap_pct", cap_d), ("max_maturity_years", mm_d)):
            if v is not None and len(dstr(v)) > 20:
                errs.append(f"{p}: {nm} has more than 20 characters after normalisation")
        key = (ac, ccy, iss)
        if key in seen:
            errs.append(f"{p}: duplicate row for {key}")
        seen.add(key)
        if len(errs) == n0 and val is not None and hc is not None and fx is not None:
            out.append({"asset_class": ac, "currency": ccy, "issuer_class": iss, "valuation_pct": dstr(val),
                        "haircut_pct": dstr(hc), "fx_haircut_pct": dstr(fx),
                        "concentration_cap_pct": dstr(cap_d) if cap_d is not None else None,
                        "cap_denominator": den if cap is not None else None, "min_rating": mr,
                        "max_maturity_years": dstr(mm_d) if mm_d is not None else None})
    return errs, (out if not errs else [])


def validate_value(key: str, value: Any, base_ccy: str) -> tuple[list[str], Any]:
    """Validate and canonicalise a field value. Returns (errors, canonical value)."""
    spec = FIELD_CATALOG.get(key)
    if spec is None:
        return [f"UNKNOWN_FIELD_KEY: {key}"], value
    k = spec["kind"]
    if k == "money":
        if not isinstance(value, dict) or set(value) != {"amount", "currency"}:
            return [f"{key}: expected {{amount, currency}}"], value
        try:
            d = to_decimal(value["amount"], field=f"{key}.amount")
        except NumericError as e:
            return [str(e)], value
        errs = []
        if value["currency"] not in SUPPORTED_CURRENCIES:
            errs.append(f"{key}: unsupported currency {value['currency']!r}")
        elif value["currency"] != base_ccy:
            errs.append(f"{key}: currency {value['currency']} differs from base currency {base_ccy} (no implicit conversion)")
        if spec.get("nonneg") and d < 0:
            errs.append(f"{key}: negative amount not permitted")
        if spec.get("positive") and d <= 0:
            errs.append(f"{key}: must be strictly positive")
        return errs, {"amount": dstr(d), "currency": value["currency"]}
    if k == "enum":
        return ([] if value in spec["values"] else [f"{key}: must be one of {sorted(spec['values'])}"]), value
    if k == "bool":
        return ([] if isinstance(value, bool) else [f"{key}: must be boolean"]), value
    if k == "str":
        return ([] if isinstance(value, str) and value.strip() else [f"{key}: non-empty string required"]), value
    if k == "list":
        e, rows = validate_schedule(value, base_ccy)
        return e, rows
    return [f"{key}: unsupported kind"], value


# --------------------------------------------------------------------------------- predicates
def rating_quality(r: Any) -> int | None:
    return -RATING_SCALE.index(r) if isinstance(r, str) and r in RATING_SCALE else None


def validate_predicate(p: Any, depth: int = 0) -> list[str]:
    """Typed, bounded predicate AST. Facts are referenced by key; no expressions, no eval."""
    if depth > 4 or not isinstance(p, dict) or not p:
        return ["predicate: invalid or too deeply nested"]
    if "all" in p or "any" in p:
        subs = p.get("all") or p.get("any")
        if not isinstance(subs, list) or not subs or len(subs) > 8:
            return ["predicate: all/any needs 1..8 operands"]
        return [e for s in subs for e in validate_predicate(s, depth + 1)]
    if "not" in p:
        return validate_predicate(p["not"], depth + 1)
    errs = []
    if p.get("op") not in {"eq", "neq", "lt", "lte", "gt", "gte", "in"}:
        errs.append(f"predicate: unsupported op {p.get('op')!r}")
    if not isinstance(p.get("fact"), str) or not p.get("fact"):
        errs.append("predicate: fact key required")
    if p.get("scale") not in (None, "RATING"):
        errs.append("predicate: unsupported scale")
    if p.get("scale") == "RATING":
        vals = p.get("value") if isinstance(p.get("value"), list) else [p.get("value")]
        if any(v not in RATING_SCALE for v in vals):
            errs.append("predicate: rating value not on the rating scale")
    return errs


def predicate_facts(p: dict) -> list[str]:
    if "all" in p or "any" in p:
        return sorted({f for s in (p.get("all") or p.get("any")) for f in predicate_facts(s)})
    if "not" in p:
        return predicate_facts(p["not"])
    return [p["fact"]]


def eval_predicate(p: dict, facts: dict[str, Any]) -> bool | None:
    """facts: key -> usable value (already filtered for effective_at <= as_of). None = UNKNOWN."""
    if "all" in p:
        rs = [eval_predicate(s, facts) for s in p["all"]]
        return False if any(r is False for r in rs) else (None if any(r is None for r in rs) else True)
    if "any" in p:
        rs = [eval_predicate(s, facts) for s in p["any"]]
        return True if any(r is True for r in rs) else (None if any(r is None for r in rs) else False)
    if "not" in p:
        r = eval_predicate(p["not"], facts)
        return None if r is None else (not r)
    if p["fact"] not in facts:
        return None
    a, b, op = facts[p["fact"]], p["value"], p["op"]
    if p.get("scale") == "RATING":
        qa = rating_quality(a)
        if qa is None:
            return None
        if op == "in":
            return qa in [rating_quality(x) for x in b]
        qb = rating_quality(b)
        return {"eq": qa == qb, "neq": qa != qb, "lt": qa < qb, "lte": qa <= qb, "gt": qa > qb, "gte": qa >= qb}[op]
    if op == "in":
        return a in b
    try:
        return {"eq": a == b, "neq": a != b, "lt": a < b, "lte": a <= b, "gt": a > b, "gte": a >= b}[op]
    except TypeError:
        return None


# --------------------------------------------------------------------------------- resolution
def usable_facts(facts: dict | None, as_of: datetime) -> dict[str, Any]:
    """Only facts with explicit source and effective_at <= as_of (no future leakage)."""
    out = {}
    for k, f in (facts or {}).items():
        if f.get("source_id") and parse_dt(f["effective_at"]) <= as_of:
            out[k] = f["value"]
    return out


def resolve_entries(entries: list[dict], as_of: datetime, facts: dict | None) -> tuple[dict, list[str], list[str]]:
    """entries: [{key, value, condition, effective_from, rank, ...}] -> (values by key, blocking, required_facts).
    Rule: among entries effective at as_of, conditional entries whose predicate is TRUE compete with unconditional
    ones; winner = highest (rank, effective_from, conditional-first). UNKNOWN predicate => BLOCK. Equal-ranked
    differing values => CONFLICT (never silently picked)."""
    uf = usable_facts(facts, as_of)
    blocking: list[str] = []
    need: set[str] = set()
    values: dict[str, Any] = {}
    for key in sorted({e["key"] for e in entries}):
        eff = [e for e in entries if e["key"] == key and parse_dt(e["effective_from"]) <= as_of]
        pool = []
        blocked = False
        for e in eff:
            if not e.get("condition"):
                pool.append((e["rank"], parse_dt(e["effective_from"]), 0, e))
                continue
            r = eval_predicate(e["condition"], uf)
            if r is None:
                miss = [f for f in predicate_facts(e["condition"]) if f not in uf]
                need.update(miss)
                blocking.append(f"UNRESOLVED_CONDITIONAL_FACT:{key}:needs {','.join(miss) or 'valid fact value'}")
                blocked = True
            elif r:
                pool.append((e["rank"], parse_dt(e["effective_from"]), 1, e))
        if blocked:
            continue
        if not pool:
            blocking.append(f"NO_TERMS_EFFECTIVE_AT_AS_OF:{key}")
            continue
        top = max(p[:3] for p in pool)
        winners = [p[3] for p in pool if p[:3] == top]
        vals = {repr(w["value"]) for w in winners}
        if len(vals) > 1:
            blocking.append(f"CONFLICTING_TERMS:{key}:{len(winners)} equal-precedence entries disagree")
            continue
        values[key] = winners[0]["value"]
    return values, blocking, sorted(need)


def freeze_blocking(entries: list[dict], field_states: list[dict], documents: list[dict]) -> list[str]:
    """Readiness for freeze. field_states: [{field_key,status,issues,...}] for ALL fields of the package."""
    out: list[str] = []
    for d in documents:
        if d.get("missing_annexes"):
            out.append(f"MISSING_ANNEX:{d['doc_ref']}:{'; '.join(d['missing_annexes'])}")
    undisposed = [f for f in field_states if f["status"] not in ("APPROVED", "REJECTED")]
    if undisposed:
        out.append(f"UNRESOLVED_FIELDS:{len(undisposed)} field(s) not approved or rejected: "
                   + ", ".join(sorted({f['field_key'] for f in undisposed})))
    for key in REQUIRED_KEYS:
        if not any(e["key"] == key and not e.get("condition") for e in entries):
            out.append(f"MISSING_REQUIRED_FIELD:{key}")
    # same-rank same-date unconditional disagreements are conflicts at freeze time already
    seen: dict[tuple, str] = {}
    for e in entries:
        if e.get("condition"):
            continue
        k = (e["key"], e["rank"], e["effective_from"])
        if k in seen and seen[k] != repr(e["value"]):
            out.append(f"CONFLICTING_TERMS:{e['key']}")
        seen[k] = repr(e["value"])
    return sorted(set(out))


def terms_output(values: dict, pkg: dict) -> dict:
    """Shape per frozen contract v1 (decimal strings)."""
    def amt(k):
        v = values.get(k)
        return v["amount"] if v else None
    return {
        "party_a_id": pkg["party_a_id"], "party_b_id": pkg["party_b_id"], "base_currency": pkg["base_currency"],
        "threshold_a": amt("threshold_a"), "threshold_b": amt("threshold_b"), "mta_a": amt("mta_a"), "mta_b": amt("mta_b"),
        "independent_amount_a": amt("independent_amount_a"), "independent_amount_b": amt("independent_amount_b"),
        "rounding_increment": amt("rounding_increment"), "rounding_mode": values.get("rounding_mode"),
        "mta_semantics": values.get("mta_semantics"), "segregated_im": values.get("segregated_im"),
        "netting_authorized": values.get("netting_authorization"),
        "valuation_agent": values.get("valuation_agent"), "call_deadline": values.get("call_deadline"),
        "eligible_schedule": values.get("eligible_schedule", []),
    }

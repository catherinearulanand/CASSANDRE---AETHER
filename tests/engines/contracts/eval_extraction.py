"""Extraction evaluation harness (spec 13.1) for the DETERMINISTIC_EXTRACTOR (RULES mode). NOT LIVE_MODEL: no model accuracy is
claimed. Documents: fixtures/contracts/eval/docs (authored for this evaluation, unseen by any extractor development).
Gold: fixtures/expected/contracts/eval_gold.json (hand-labelled, independent; never imported by production code).

Outcome taxonomy per gold field (a 'safe' outcome never lets a wrong value look approved-ready):
  CORRECT          clean proposal (no issues), value + scope equal to gold                         -> exact match
  FLAGGED          proposal exists but carries issues (UNRESOLVED)                                  -> safe miss / safe flag
  SURFACED_UNPARSED the clause was not parsed but an UNPARSED_MATERIAL_CLAUSE item overlaps it      -> safe, surfaced for review
  WRONG_CLEAN      clean proposal with a value/scope different from gold                           -> SILENT ERROR
  OMITTED          no proposal and nothing surfaced                                                -> silent omission (blocked only if a required field)
For gold fields with must_flag=true the only success is FLAGGED or SURFACED_UNPARSED.

Run:  python tests/engines/contracts/eval_extraction.py [--tag NAME]   (writes reports/test-results/contracts_extraction_eval[.NAME].json)
"""
from __future__ import annotations

import json
import math
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.errors import ValidationFailed  # noqa: E402
from app.engines.contracts import extract as X  # noqa: E402
from app.engines.contracts import services as S  # noqa: E402
from app.engines.contracts import terms as T  # noqa: E402

SETS = {  # name -> (documents dir, gold file)
    "development": (ROOT / "fixtures" / "contracts" / "eval" / "docs", ROOT / "fixtures" / "expected" / "contracts" / "eval_gold.json"),
    "held_out": (ROOT / "fixtures" / "contracts" / "eval" / "heldout", ROOT / "fixtures" / "expected" / "contracts" / "eval_gold_heldout.json"),
}
DOCS = SETS["development"][0]
STRUCTURAL = ("QUOTE_OFFSET_MISMATCH", "UNKNOWN_FIELD_KEY", "VALUE_MISSING", "VALIDATION:", "NON_ASCII_DOCUMENT_REVIEW")


def load_text(name: str, docs_dir: Path | None = None) -> str:
    return ((docs_dir or DOCS) / f"{name}.txt").read_bytes().replace(b"\r\n", b"\n").decode("utf-8")


def wilson(k: int, n: int, z: float = 1.96) -> list[float] | None:
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(max(0.0, c - h), 4), round(min(1.0, c + h), 4)]


def rate(k: int, n: int) -> dict:
    return {"k": k, "n": n, "rate": round(k / n, 4) if n else None, "wilson95": wilson(k, n)}


# ---------------------------------------------------------------- normalisation (independent of the production validators)
def norm_value(key: str, v):
    if v is None:
        return None
    if key == "eligible_schedule":
        return [{k: (None if r[k] is None else (str(Decimal(str(r[k]))) if k.endswith("_pct") or k == "max_maturity_years"
                                                  else str(r[k]).upper())) for k in sorted(r)} for r in v]
    if isinstance(v, str) and ":" in v and v[:3].isalpha() and v[3] == ":":  # gold money shorthand CCY:amount
        c, a = v.split(":", 1)
        return {"amount": str(Decimal(a).normalize()), "currency": c}
    if isinstance(v, dict) and set(v) == {"amount", "currency"}:
        return {"amount": str(Decimal(str(v["amount"])).normalize()), "currency": v["currency"]}
    return v


def span_of(text: str, quote: str) -> tuple[int, int]:
    i = text.find(quote)
    if i < 0:
        raise AssertionError(f"gold quote not found in document (authoring error): {quote[:60]!r}")
    return i, i + len(quote)


def overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def line_span(text: str, quote: str) -> tuple[int, int]:
    i = text.find(quote)
    if i < 0:
        raise AssertionError(f"injection marker not found: {quote!r}")
    s = text.rfind("\n", 0, i) + 1
    e = text.find("\n", i)
    return s, (len(text) if e < 0 else e)


# ---------------------------------------------------------------- pipeline under test (same functions the service uses)
def run_pipeline(text: str, a: str, b: str, base: str) -> list[dict]:
    props = X.extract_rules(text, {"id": "ENT-EVAL-A", "name": a}, {"id": "ENT-EVAL-B", "name": b}, base)
    out = []
    for p in props:
        norm, issues = X.check_proposal(p, text, a, b, base, None)
        out.append({"key": p["field_key"], "direction": p.get("direction"), "value": norm.get("value"), "condition": p.get("condition"),
                    "span": (p["start"], p["end"]), "quote": p["quote"], "issues": issues, "status": "UNRESOLVED" if issues else "PROPOSED"})
    return out


def eval_doc(g: dict, base: str, docs_dir: Path | None = None) -> dict:
    name = g["doc"]
    text = load_text(name, docs_dir)
    res: dict = {"doc": name, "layout": g["layout"], "fields": [], "forbidden": [], "injection": [], "freeze": None}
    # intake (header scope): the same validator the package-creation path uses
    hdr = X.header_info(text)
    res["header_effective_date_ok"] = hdr["effective_date"] == g["header"]["effective_date"]
    try:
        S._validate_doc(0, {"kind": "CSA", "doc_ref": name, "title": name, "text": text,
                            "effective_from": g["header"]["effective_date"] + "T00:00:00Z"}, (g["party_a"], g["party_b"]))
        intake = "ACCEPT"
    except ValidationFailed as e:
        intake = "REJECT_SWAPPED_PARTIES" if "DOC_PARTY_MISMATCH" in str(e.field_errors) else "REJECT_OTHER"
    res["intake_expected"], res["intake_actual"] = g["intake"], intake
    if intake != "ACCEPT":
        res["proposals"] = 0
        return res
    props = run_pipeline(text, g["party_a"], g["party_b"], base)
    res["proposals"] = len(props)
    gspans = []
    for gf in g["fields"]:
        gsp = span_of(text, gf["quote"])
        gspans.append((gf["key"], gsp))
        # Candidate = same field key whose citation overlaps the gold clause. Direction is a SCOPE attribute scored below:
        # a flagged proposal with an unrecognised party is still a surfaced item; a CLEAN proposal with the wrong direction
        # is a silent scope error (WRONG_CLEAN).
        cands = [p for p in props if p["key"] == gf["key"] and overlap(p["span"], gsp)]
        unparsed = [p for p in props if p["key"] == "unparsed_clause" and overlap(p["span"], gsp)]
        if not cands:  # a FLAGGED proposal under another key (e.g. unknown party => side-less key) still surfaces the clause
            cands = [p for p in props if p["key"] != "unparsed_clause" and p["issues"] and overlap(p["span"], gsp)]
        gv, must = norm_value(gf["key"], gf.get("value")), bool(gf.get("must_flag"))
        gcond = gf.get("condition")
        if not cands:
            outcome, cit = ("SURFACED_UNPARSED" if unparsed else "OMITTED"), bool(unparsed)
        else:
            # prefer a candidate with matching condition presence; then clean over flagged
            cands.sort(key=lambda p: ((p["condition"] is not None) != (gcond is not None), bool(p["issues"])))
            p = cands[0]
            cit = True
            if p["issues"]:
                outcome = "FLAGGED"
            elif norm_value(gf["key"], p["value"]) == gv and p["condition"] == gcond and p["direction"] == gf["direction"]:
                outcome = "CORRECT"
            else:
                outcome = "WRONG_CLEAN"
        scope_ok = None
        if cands:
            scope_ok = (cands[0]["direction"] == gf["direction"]) and (cands[0]["condition"] == gcond)
        res["fields"].append({"key": gf["key"], "direction": gf["direction"], "must_flag": must, "outcome": outcome,
                              "cited": cit, "scope_ok": scope_ok})
    for ff in g.get("forbidden", []):
        sp = span_of(text, ff["quote"])
        hit = [p for p in props if p["key"] == ff["key"] and overlap(p["span"], sp)]
        res["forbidden"].append({"key": ff["key"], "outcome": "ABSENT" if not hit else ("FLAGGED" if all(p["issues"] for p in hit) else "UNFLAGGED_PROPOSAL")})
    for iq in g.get("injection_quotes", []):
        ls = line_span(text, iq)
        hit = [p for p in props if overlap(p["span"], ls)]
        res["injection"].append({"marker": iq, "operative_proposals_from_instruction_text": sum(1 for p in hit if p["key"] != "unparsed_clause"),
                                 "surfaced_for_review": sum(1 for p in hit if p["key"] == "unparsed_clause" and p["issues"])})
    # proposal-level citation precision + spurious
    allg = gspans + [(f["key"], span_of(text, f["quote"])) for f in g.get("forbidden", [])]
    sup = spur = 0
    for p in props:
        if p["key"] == "unparsed_clause":
            continue
        if any(k == p["key"] and overlap(p["span"], sp) for k, sp in allg):
            sup += 1
        else:
            spur += 1
    res["proposals_supported"], res["proposals_spurious"] = sup, spur
    res["proposals_structurally_invalid"] = sum(1 for p in props if any(i.startswith(STRUCTURAL) for i in p["issues"]))
    res["proposals_flagged"] = sum(1 for p in props if p["issues"])
    res["unparsed_items"] = sum(1 for p in props if p["key"] == "unparsed_clause")
    # reviewer-simulation freeze readiness: approve exactly the clean proposals, reject the flagged ones
    entries = [{"key": p["key"], "direction": p["direction"], "value": p["value"], "condition": p["condition"],
                "effective_from": g["header"]["effective_date"], "rank": 3} for p in props if not p["issues"] and p["key"] != "unparsed_clause"]
    states = [{"field_key": p["key"], "status": "APPROVED" if not p["issues"] and p["key"] != "unparsed_clause" else "REJECTED"} for p in props]
    blocking = T.freeze_blocking(entries, states, [])
    net = [e for e in entries if e["key"] == "netting_authorization" and not e["condition"]]
    if net and not any(e["value"] is True for e in net):  # mirrors services._candidate
        blocking = sorted(set(blocking) | {"NETTING_NOT_AUTHORISED"})
    exp = g.get("expect_freeze_blocking", [])
    anyof = lambda e: e if isinstance(e, list) else [e]  # noqa: E731  (an expectation may list acceptable alternatives)
    found = [e for e in exp if any(b.startswith(a) for a in anyof(e) for b in blocking)]
    res["freeze"] = {"expected": exp, "actual_blocking": blocking, "expected_found": found}
    return res


def aggregate(per_doc: list[dict]) -> dict:
    fields = [f for d in per_doc for f in d["fields"]]
    normal = [f for f in fields if not f["must_flag"]]
    must = [f for f in fields if f["must_flag"]]
    cnt = lambda lst, o: sum(1 for f in lst if f["outcome"] == o)  # noqa: E731
    accepted = [d for d in per_doc if d["intake_actual"] == "ACCEPT"]
    forb = [x for d in per_doc for x in d["forbidden"]]
    inj = [x for d in per_doc for x in d["injection"]]
    props = sum(d["proposals"] for d in accepted)
    sup = sum(d.get("proposals_supported", 0) for d in accepted)
    spur = sum(d.get("proposals_spurious", 0) for d in accepted)
    invalid = sum(d.get("proposals_structurally_invalid", 0) for d in accepted)
    flagged = sum(d.get("proposals_flagged", 0) for d in accepted)
    cited = sum(1 for f in fields if f["cited"])
    scope = [f for f in fields if f["scope_ok"] is not None]
    exp_codes = [(d["doc"], e) for d in per_doc if d["freeze"] for e in d["freeze"]["expected"]]
    found = sum(len(d["freeze"]["expected_found"]) for d in per_doc if d["freeze"])
    by_key: dict = {}
    for f in fields:
        k = by_key.setdefault(f["key"], {"CORRECT": 0, "FLAGGED": 0, "SURFACED_UNPARSED": 0, "WRONG_CLEAN": 0, "OMITTED": 0, "gold_n": 0})
        k[f["outcome"]] += 1
        k["gold_n"] += 1
    silent_wrong = cnt(normal, "WRONG_CLEAN") + sum(1 for f in must if f["outcome"] in ("WRONG_CLEAN", "CORRECT"))
    return {
        "documents": {"n": len(per_doc), "accepted_at_intake": len(accepted), "rejected_at_intake": len(per_doc) - len(accepted)},
        "gold_fields": {"total": len(fields), "normal": len(normal), "must_flag": len(must)},
        "intake_accuracy": rate(sum(1 for d in per_doc if d["intake_expected"] == d["intake_actual"]), len(per_doc)),
        "header_effective_date_accuracy": rate(sum(1 for d in per_doc if d["header_effective_date_ok"]), len(per_doc)),
        "field_exact_match_after_normalisation": rate(cnt(normal, "CORRECT"), len(normal)),
        "normal_fields_outcomes": {o: cnt(normal, o) for o in ("CORRECT", "FLAGGED", "SURFACED_UNPARSED", "WRONG_CLEAN", "OMITTED")},
        "silent_errors": {"wrong_clean_value_normal_fields": cnt(normal, "WRONG_CLEAN"),
                          "must_flag_fields_presented_clean": sum(1 for f in must if f["outcome"] in ("WRONG_CLEAN", "CORRECT")),
                          "forbidden_clauses_proposed_unflagged": sum(1 for x in forb if x["outcome"] == "UNFLAGGED_PROPOSAL"),
                          "total": silent_wrong + sum(1 for x in forb if x["outcome"] == "UNFLAGGED_PROPOSAL")},
        "silent_omissions": {"normal_fields": cnt(normal, "OMITTED"), "must_flag_fields": cnt(must, "OMITTED")},
        "citation_recall": rate(cited, len(fields)),
        "citation_precision": rate(sup, sup + spur),
        "spurious_proposals_overlapping_no_gold": spur,
        "scope_accuracy_direction_and_condition": rate(sum(1 for f in scope if f["scope_ok"]), len(scope)),
        "invalid_output_rate_structural": rate(invalid, props),
        "flag_rate_all_proposals": rate(flagged, props),
        "unresolved_material_field_recall": rate(sum(1 for f in must if f["outcome"] in ("FLAGGED", "SURFACED_UNPARSED")), len(must)),
        "required_field_gap_detection_at_freeze": rate(found, len(exp_codes)),
        "forbidden_clause_handling": {o: sum(1 for x in forb if x["outcome"] == o) for o in ("ABSENT", "FLAGGED", "UNFLAGGED_PROPOSAL")},
        "prompt_injection": {"markers": len(inj), "operative_proposals_from_instruction_text": sum(x["operative_proposals_from_instruction_text"] for x in inj),
                             "surfaced_for_review_as_unresolved": sum(1 for x in inj if x["surfaced_for_review"] > 0)},
        "unparsed_material_clause_items": sum(d.get("unparsed_items", 0) for d in accepted),
        "per_field_confusion": dict(sorted(by_key.items())),
        "reviewer_correction_time": None,
    }


def run_set(name: str) -> dict:
    docs_dir, gold_file = SETS[name]
    gold = json.loads(gold_file.read_text(encoding="utf-8"))
    per_doc = [eval_doc(g, gold["package"]["base_currency"], docs_dir) for g in gold["docs"]]
    return {"gold_file": str(gold_file.relative_to(ROOT)).replace("\\", "/"), "aggregate": aggregate(per_doc), "per_document": per_doc}


def run_eval() -> dict:
    baseline_file = ROOT / "reports" / "test-results" / "contracts_extraction_eval.baseline.json"
    baseline = json.loads(baseline_file.read_text(encoding="utf-8")) if baseline_file.exists() else None
    return {
        "mode": "DETERMINISTIC_EXTRACTOR", "live_model": False,
        "claim": "Results describe a regular-expression clause extractor on small hand-authored synthetic document sets. They are NOT a "
                 "model-accuracy claim and do not generalise to real agreements. Small samples: see the Wilson 95% intervals.",
        "how_to_read": {
            "baseline_before_hardening": "Development set measured BEFORE the extractor hardening (the honest 'unseen' result on those 18 documents).",
            "development_set_after_hardening": "Same 18 documents AFTER hardening designed from the baseline failures: OPTIMISTIC (the extractor was tuned on them).",
            "held_out_set": "8 documents authored AFTER the hardening with variations it was not designed for: the fairer estimate of current behaviour."},
        "reviewer_correction_time_note": "Not measured: no human reviewer study was run.",
        "evaluated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "baseline_before_hardening": (baseline or {}).get("aggregate"),
        "development_set_after_hardening": run_set("development"),
        "held_out_set": run_set("held_out"),
    }


if __name__ == "__main__":
    rep = run_eval()
    out = ROOT / "reports" / "test-results" / "contracts_extraction_eval.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    KEYS = ("documents", "gold_fields", "field_exact_match_after_normalisation", "normal_fields_outcomes", "silent_errors", "silent_omissions",
            "citation_recall", "citation_precision", "scope_accuracy_direction_and_condition", "invalid_output_rate_structural",
            "unresolved_material_field_recall", "required_field_gap_detection_at_freeze", "forbidden_clause_handling", "prompt_injection")
    for title, agg in (("BASELINE (dev set, before hardening)", rep["baseline_before_hardening"]),
                       ("DEV SET after hardening (optimistic)", rep["development_set_after_hardening"]["aggregate"]),
                       ("HELD-OUT set", rep["held_out_set"]["aggregate"])):
        print("==", title)
        for k in KEYS:
            if agg and k in agg:
                print("  ", k, json.dumps(agg[k]))
    print("wrote", out)

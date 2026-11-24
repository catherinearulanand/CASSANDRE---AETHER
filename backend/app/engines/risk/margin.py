"""A3 VECTOR OTC margin waterfall (pure). Illustrative simple one-direction VM model; NOT SIMM, NOT certified.

Perspective: Party A = our institution, Party B = counterparty. Exposure E > 0 means B owes A (A is the secured party,
B is the obligor posting collateral). The agreement's directional terms (threshold_x, mta_x, independent_amount_x) apply
to the OBLIGOR for the current direction.

  target      = max(0, |E| + IA_obligor - Threshold_obligor)
  delta       = target - collateral already held from the obligor (same direction)
  delta > 0   -> CALL  (obligor posts);   delta < 0 -> RETURN (secured party returns excess)
  MTA test    : on the UNROUNDED |delta|, semantics GTE (>=) or GT (>) as stated in the terms
  rounding    : agreement-specific; CALL uses terms.rounding_mode, RETURN uses terms.return_rounding_mode
                (if absent: DOWN, flagged as an explicit assumption step - never returns more than the excess)
Collateral posted by the OPPOSITE party is returned as a SEPARATE obligation and never offsets the new direction.
IM (segregated) is a separate illustrative schedule and is never netted against VM.
"""
from __future__ import annotations

from decimal import Decimal, localcontext

from app.core.money import NumericError, dstr, round_to_increment, to_decimal
from app.engines.risk.pricing import PREC

REQUIRED_TERMS = ("base_currency", "threshold_a", "threshold_b", "mta_a", "mta_b", "independent_amount_a",
                  "independent_amount_b", "rounding_increment", "rounding_mode", "mta_semantics")
ZERO = Decimal(0)


class TermsError(ValueError):
    pass


def validate_terms(terms: dict) -> dict:
    """No silent defaults for material terms: missing/invalid => TermsError listing every problem."""
    errs = [f"missing material term: {k}" for k in REQUIRED_TERMS if terms.get(k) in (None, "")]
    out: dict = {}
    if not errs:
        try:
            for k in ("threshold_a", "threshold_b", "mta_a", "mta_b", "independent_amount_a", "independent_amount_b"):
                out[k] = to_decimal(terms[k], field=k, allow_negative=False)
            out["rounding_increment"] = to_decimal(terms["rounding_increment"], field="rounding_increment")
            if out["rounding_increment"] <= 0:
                errs.append("rounding_increment must be positive")
        except NumericError as exc:
            errs.append(str(exc))
        if terms["rounding_mode"] not in ("UP", "DOWN", "HALF_UP", "HALF_EVEN"):
            errs.append(f"unsupported rounding_mode {terms['rounding_mode']!r}")
        if terms["mta_semantics"] not in ("GTE", "GT"):
            errs.append(f"unsupported mta_semantics {terms['mta_semantics']!r}")
        rm = terms.get("return_rounding_mode")
        if rm is not None and rm not in ("UP", "DOWN", "HALF_UP", "HALF_EVEN"):
            errs.append(f"unsupported return_rounding_mode {rm!r}")
    if errs:
        raise TermsError("; ".join(errs))
    out.update(base_currency=terms["base_currency"], rounding_mode=terms["rounding_mode"], mta_semantics=terms["mta_semantics"],
               return_rounding_mode=terms.get("return_rounding_mode"), segregated_im=bool(terms.get("segregated_im", False)))
    return out


def _mta_hit(amount: Decimal, mta: Decimal, sem: str) -> bool:
    return amount >= mta if sem == "GTE" else amount > mta


def otc_waterfall(exposure: Decimal, terms: dict, held: dict, ccy: str, terms_hash: str) -> dict:
    """held = {"PARTY_A": collateral posted BY A currently held, "PARTY_B": ... by B}. Returns primary obligation + steps."""
    t = validate_terms(terms)
    if t["base_currency"] != ccy:
        raise TermsError(f"terms base currency {t['base_currency']} != valuation currency {ccy}")
    steps: list[dict] = []
    cite = lambda k: f"terms[{terms_hash[:12]}].{k}"  # noqa: E731

    def step(name, value, citation):
        steps.append({"step": name, "value": dstr(value) if isinstance(value, Decimal) else str(value), "citation": citation})

    with localcontext() as c:
        c.prec = PREC
        hA, hB = to_decimal(held.get("PARTY_A", "0"), field="held.PARTY_A", allow_negative=False), \
            to_decimal(held.get("PARTY_B", "0"), field="held.PARTY_B", allow_negative=False)
        step("net exposure (signed, + means counterparty owes us)", exposure, "netting set PV")
        if exposure > 0:
            obligor, secured, sfx = "PARTY_B", "PARTY_A", "b"
            same, opp = hB, hA
        elif exposure < 0:
            obligor, secured, sfx = "PARTY_A", "PARTY_B", "a"
            same, opp = hA, hB
        else:
            obligor = secured = None
            sfx, same, opp = "b", ZERO, ZERO
        out = {"obligor": obligor, "secured_party": secured, "direction": "NONE", "unrounded_amount": ZERO, "rounded_amount": ZERO,
               "target": ZERO, "held_same_direction": same, "residual": ZERO, "opposite_return": ZERO, "steps": steps,
               "mta_triggered": False}
        if obligor is None:
            step("direction", "NONE (zero exposure: no obligor)", "derived")
            out["opposite_return"] = hA + hB
            out["opposite_detail"] = {"PARTY_A": hA, "PARTY_B": hB}
            if hA + hB > 0:
                step("collateral held both ways returned separately (no netting)", hA + hB, "SEPARATE_OBLIGATION")
            return out
        thr, ia, mta = t[f"threshold_{sfx}"], t[f"independent_amount_{sfx}"], t[f"mta_{sfx}"]
        step(f"obligor / secured party", f"{obligor} posts to {secured}", "derived from sign of exposure")
        step("threshold (obligor)", thr, cite(f"threshold_{sfx}"))
        step("independent amount (obligor)", ia, cite(f"independent_amount_{sfx}"))
        target = max(ZERO, abs(exposure) + ia - thr)
        step("target collateral = max(0, |E| + IA - Threshold)", target, "illustrative VM formula")
        step("collateral already held from obligor (same direction)", same, "context.existing_vm_balance")
        delta = target - same
        step("delta = target - held (unrounded)", delta, "derived")
        out.update(target=target)
        if delta == 0:
            step("result", "NONE (delta is zero)", "derived")
        else:
            amt = abs(delta)
            hit = _mta_hit(amt, mta, t["mta_semantics"])
            step(f"MTA test on UNROUNDED |delta| ({t['mta_semantics']}) vs MTA", f"{dstr(amt)} vs {dstr(mta)} -> {'TRIGGERED' if hit else 'NOT TRIGGERED'}",
                 cite("mta_" + sfx) + f" / {cite('mta_semantics')}")
            if not hit:
                out["unrounded_amount"], out["residual"] = amt, amt
                out["direction_candidate"] = "POST" if delta > 0 else "RETURN"
            else:
                is_call = delta > 0
                if is_call:
                    mode = t["rounding_mode"]
                    step("rounding mode (call)", mode, cite("rounding_mode"))
                else:
                    mode = t["return_rounding_mode"] or "DOWN"
                    if t["return_rounding_mode"]:
                        step("rounding mode (return)", mode, cite("return_rounding_mode"))
                    else:
                        step("rounding mode (return)", "DOWN", "ASSUMPTION: terms carry no return_rounding_mode; DOWN never returns more than the excess")
                rounded = round_to_increment(amt, t["rounding_increment"], mode)
                if not is_call and rounded > same:
                    rounded = same
                step(f"rounded to increment {dstr(t['rounding_increment'])}", rounded, cite("rounding_increment"))
                out["unrounded_amount"], out["rounded_amount"], out["residual"] = amt, rounded, rounded - amt
                if rounded > 0:
                    out["direction"] = "POST" if is_call else "RETURN"
                    out["mta_triggered"] = True
        # collateral from the opposite party: separate obligation, never offset
        if opp > 0:
            opp_poster = secured  # the opposite party is the secured party, who previously posted
            osfx = "a" if opp_poster == "PARTY_A" else "b"
            omta = t[f"mta_{osfx}"]
            if _mta_hit(opp, omta, t["mta_semantics"]):
                r = round_to_increment(opp, t["rounding_increment"], "DOWN")
                out["opposite_return"] = r
                out["opposite_detail"] = {"poster": opp_poster, "held": dstr(opp)}
                step(f"opposite-direction collateral posted by {opp_poster} returned SEPARATELY (not netted)", r,
                     "SEPARATE_OBLIGATION; MTA of poster applied; DOWN rounding assumption")
            else:
                step("opposite-direction collateral below MTA: no return", opp, "SEPARATE_OBLIGATION")
        return out


def im_illustrative(trade_notionals: list[tuple[Decimal, Decimal]], schedule: list[dict]) -> Decimal:
    """Demonstrative schedule IM = sum |notional_base| * rate(bucket). Not SIMM. trade_notionals = [(abs_notional, residual_years)]."""
    sched = sorted(((to_decimal(s["max_years"], field="max_years"), to_decimal(s["rate"], field="rate")) for s in schedule), key=lambda x: x[0])
    tot = ZERO
    with localcontext() as c:
        c.prec = PREC
        for n, yrs in trade_notionals:
            rate = next((r for m, r in sched if yrs <= m), sched[-1][1])
            tot += abs(n) * rate
    return tot

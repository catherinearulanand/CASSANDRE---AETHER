"""A3 VECTOR: synthetic clearing-style margin file parsing + reconciliation (pure).

CSV columns (UTF-8, strict decimals): file_id,account_id,component,amount,currency,aggregation_kind,as_of
aggregation_kind:
  ADDITIVE        summed into the independent total
  OFFSET          subtracted (e.g. a reported offset/credit component)
  INFO            memo line, never summed (avoids double counting)
  REPORTED_TOTAL  the total the SOURCE file claims; compared to the independent sum, never summed itself
Imported margin is the SOURCE OBLIGATION; it is not an internally computed SPAN figure.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from decimal import Decimal

from app.core.money import SUPPORTED_CURRENCIES, NumericError, dstr, to_decimal

HEADER = ["file_id", "account_id", "component", "amount", "currency", "aggregation_kind", "as_of"]
KINDS = {"ADDITIVE", "OFFSET", "INFO", "REPORTED_TOTAL"}
MAX_ROWS = 100_000


class ClearingFileError(Exception):
    def __init__(self, errors: list[dict]):
        super().__init__(f"{len(errors)} row error(s)")
        self.errors = errors


def parse_file(csv_text: str) -> dict:
    """Atomic: any row error rejects the whole file (no silent partial import). Returns normalised file dict."""
    errs: list[dict] = []
    rdr = csv.reader(io.StringIO(csv_text))
    try:
        header = next(rdr)
    except StopIteration:
        raise ClearingFileError([{"row": 0, "field": "file", "error": "empty file"}])
    if [h.strip() for h in header] != HEADER:
        raise ClearingFileError([{"row": 1, "field": "header", "error": f"expected header {','.join(HEADER)}"}])
    rows, seen = [], set()
    file_ids, accounts, currencies, as_ofs = set(), set(), set(), set()
    for i, r in enumerate(rdr, start=2):
        if i - 1 > MAX_ROWS:
            raise ClearingFileError([{"row": i, "field": "file", "error": f"more than {MAX_ROWS} rows"}])
        if not r or all(not c.strip() for c in r):
            continue
        if len(r) != len(HEADER):
            errs.append({"row": i, "field": "row", "error": f"expected {len(HEADER)} columns, got {len(r)}"})
            continue
        fid, acc, comp, amt, ccy, kind, as_of = [c.strip() for c in r]
        if any(c.startswith(("=", "+", "-", "@")) for c in (fid, acc, comp)):
            errs.append({"row": i, "field": "text", "error": "leading =,+,-,@ rejected (formula injection)"})
        try:
            a = to_decimal(amt, field="amount")
        except NumericError as exc:
            errs.append({"row": i, "field": "amount", "error": str(exc)})
            continue
        if ccy not in SUPPORTED_CURRENCIES:
            errs.append({"row": i, "field": "currency", "error": f"unsupported currency {ccy!r}"})
        if kind not in KINDS:
            errs.append({"row": i, "field": "aggregation_kind", "error": f"must be one of {sorted(KINDS)}"})
        if kind in ("ADDITIVE", "OFFSET", "REPORTED_TOTAL") and a < 0:
            errs.append({"row": i, "field": "amount", "error": "negative amount not permitted for this aggregation kind (use OFFSET)"})
        try:
            ts = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                raise ValueError("timezone required")
            ts = ts.astimezone(timezone.utc)
        except ValueError as exc:
            errs.append({"row": i, "field": "as_of", "error": f"invalid ISO-8601 UTC timestamp: {exc}"})
            continue
        key = (acc, comp)
        if key in seen:
            errs.append({"row": i, "field": "component", "error": f"duplicate component {comp!r} for account {acc!r}"})
        seen.add(key)
        file_ids.add(fid); accounts.add(acc); currencies.add(ccy); as_ofs.add(ts)
        rows.append({"component": comp, "amount": a, "currency": ccy, "kind": kind, "row": i})
    if len(file_ids) > 1:
        errs.append({"row": 0, "field": "file_id", "error": "multiple file_ids in one file"})
    if len(accounts) > 1:
        errs.append({"row": 0, "field": "account_id", "error": "multiple accounts in one file (account grouping must be unique)"})
    if len(currencies) > 1:
        errs.append({"row": 0, "field": "currency", "error": "mixed currencies in one file: no currency blending"})
    if len(as_ofs) > 1:
        errs.append({"row": 0, "field": "as_of", "error": "multiple as_of values in one file"})
    if sum(1 for x in rows if x["kind"] == "REPORTED_TOTAL") > 1:
        errs.append({"row": 0, "field": "aggregation_kind", "error": "more than one REPORTED_TOTAL row"})
    if not rows:
        errs.append({"row": 0, "field": "file", "error": "no data rows"})
    if errs:
        raise ClearingFileError(errs)
    return {"file_id": file_ids.pop(), "account_id": accounts.pop(), "currency": currencies.pop(), "as_of": as_ofs.pop(), "rows": rows}


def reconcile(rows: list[dict], declared_total: Decimal | None) -> dict:
    """Independent sum of additive/offset components vs the file's reported total (row or declared header)."""
    add = sum((r["amount"] for r in rows if r["kind"] == "ADDITIVE"), Decimal(0))
    off = sum((r["amount"] for r in rows if r["kind"] == "OFFSET"), Decimal(0))
    computed = add - off
    reported = next((r["amount"] for r in rows if r["kind"] == "REPORTED_TOTAL"), None)
    if reported is None:
        reported = declared_total
    if reported is None:
        return {"status": "EXCEPTION", "computed_total": computed, "reported_total": None, "difference": None,
                "reason": "no reported total supplied: cannot reconcile (UNKNOWN, not PASS)"}
    diff = reported - computed
    return {"status": "RECONCILED" if diff == 0 else "EXCEPTION", "computed_total": computed, "reported_total": reported,
            "difference": diff, "reason": None if diff == 0 else f"reported total differs from independent component sum by {dstr(diff)}"}

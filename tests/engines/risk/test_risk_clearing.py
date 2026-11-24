"""V-A06/V-A07: clearing import arithmetic vs independent totals; duplicate/corrupt files blocked; G10."""
import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.engines.risk import clearing as clr

D = Decimal
HDR = "file_id,account_id,component,amount,currency,aggregation_kind,as_of\n"
EXP = json.loads((Path(__file__).resolve().parents[3] / "fixtures" / "expected" / "risk" / "hand_cases.json").read_text())["golden"]["G10"]


def row(comp, amt, kind="ADDITIVE", acc="ACC-DEMO-NSE-001", ccy="INR"):
    return f"F1,{acc},{comp},{amt},{ccy},{kind},2026-08-17T03:30:00Z\n"


def test_g10_components_sum_to_independent_total_and_reconcile():
    f = clr.parse_file(HDR + row("A", EXP["components"][0]) + row("B", EXP["components"][1]) + row("T", EXP["total"], "REPORTED_TOTAL"))
    rec = clr.reconcile(f["rows"], None)
    assert rec["computed_total"] == D(EXP["total"]) and rec["status"] == "RECONCILED" and rec["difference"] == 0


def test_g10_supplied_total_mismatch_is_exception():
    f = clr.parse_file(HDR + row("A", "8000000") + row("B", "2000000") + row("T", EXP["supplied_bad_total"], "REPORTED_TOTAL"))
    rec = clr.reconcile(f["rows"], None)
    assert rec["status"] == "EXCEPTION" and rec["difference"] == D("100000")


def test_info_rows_are_not_summed_and_offsets_subtract():
    f = clr.parse_file(HDR + row("A", "100") + row("INFO1", "999", "INFO") + row("CREDIT", "30", "OFFSET") + row("T", "70", "REPORTED_TOTAL"))
    rec = clr.reconcile(f["rows"], None)
    assert rec["computed_total"] == 70 and rec["status"] == "RECONCILED"


def test_no_reported_total_is_exception_not_pass():
    f = clr.parse_file(HDR + row("A", "100"))
    assert clr.reconcile(f["rows"], None)["status"] == "EXCEPTION"
    assert clr.reconcile(f["rows"], D("100"))["status"] == "RECONCILED"  # declared header total accepted


@pytest.mark.parametrize("body,needle", [
    (row("A", "1") + row("A", "2"), "duplicate"),
    (row("A", "abc"), "decimal"),
    (row("A", "NaN"), "NaN"),
    (row("A", "1", ccy="XXX"), "currency"),
    (row("=cmd", "1"), "formula"),
    (row("A", "1", "BOGUS"), "aggregation_kind"),
    (row("A", "-5"), "negative"),
    (row("A", "1") + row("B", "1", acc="OTHER"), "multiple accounts"),
    (row("A", "1") + row("B", "1", ccy="USD"), "mixed currencies"),
])
def test_atomic_rejection_of_bad_rows(body, needle):
    with pytest.raises(clr.ClearingFileError) as e:
        clr.parse_file(HDR + body)
    assert needle.lower() in json.dumps(e.value.errors).lower()


def test_bad_header_and_empty_rejected():
    with pytest.raises(clr.ClearingFileError):
        clr.parse_file("a,b\n1,2\n")
    with pytest.raises(clr.ClearingFileError):
        clr.parse_file("")
    with pytest.raises(clr.ClearingFileError):
        clr.parse_file(HDR)

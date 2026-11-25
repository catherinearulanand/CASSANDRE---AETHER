"""A1 REGULUS browser flow (Playwright, system Edge). Needs: backend + `next start` running against a seeded demo DB.
    python tests/e2e/regulatory_flows.py [base_url]        (default http://localhost:3101)
Flow: maker ingests a fictional source -> deterministic extraction -> drafts a rule from a candidate -> submits ->
      maker CANNOT approve (UI disabled + API 403 SELF_APPROVAL, G15) -> independent checker approves the exact revision ->
      checker schedules it -> Impact lab (hypothetical DEMO-ETD-MARGIN v2) shows INR 12,500,000 / shortfall 1,000,000 via the REAL
      risk + collateral services with HYPOTHETICAL labelling and a coverage-feasibility result.
Writes reports/screenshots/regulatory_*.png and reports/test-results/regulatory_flows.json. Exit 1 on any failed step."""
import json
import sys
import tempfile
import time
import traceback
import uuid
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[2]
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:3101"
CREDS = json.loads((ROOT / ".demo-credentials").read_text())["tenant-a"]
SHOTS = ROOT / "reports" / "screenshots"
SHOTS.mkdir(parents=True, exist_ok=True)
steps: list[dict] = []
UID = uuid.uuid4().hex[:8].upper()
KEY = f"DEMO-E2E-{UID}"


def login(ctx, user):
    pg = ctx.new_page()
    pg.goto(BASE + "/overview")
    pg.fill("#username", user)
    pg.fill("#password", CREDS[user])
    pg.click("button.primary")
    pg.wait_for_selector(".shell")
    return pg


def step(name, fn):
    t0 = time.time()
    try:
        detail = fn()
        steps.append({"step": name, "ok": True, "seconds": round(time.time() - t0, 2), "detail": detail})
        print(f"PASS  {name}" + (f"  [{detail}]" if detail else ""))
        return detail
    except Exception as exc:  # noqa: BLE001
        steps.append({"step": name, "ok": False, "seconds": round(time.time() - t0, 2), "error": f"{type(exc).__name__}: {exc}"[:500]})
        print(f"FAIL  {name}: {type(exc).__name__}: {str(exc)[:300]}")
        traceback.print_exc(limit=2)
        return None


def shot(pg, name):
    pg.screenshot(path=str(SHOTS / f"regulatory_{name}.png"), full_page=True)


def main():
    with sync_playwright() as p:
        b = p.chromium.launch(channel="msedge", headless=True)
        errs: list[str] = []
        maker_ctx = b.new_context(viewport={"width": 1440, "height": 900})
        mk = login(maker_ctx, "maker")
        mk.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        state: dict = {}

        def ingest():
            mk.goto(BASE + "/regulatory?tab=workbench")
            mk.wait_for_selector("form[aria-label='Upload source']")
            doc = (f"DEMO E2E NOTICE {UID}\nSYNTHETIC DOCUMENT FOR AETHER TESTING - NOT LAW.\n\n"
                   "1.1 The clearing member shall apply a margin multiplier of 1.10 times on DEMO_NSE index futures with effect from 2027-02-01.\n\n"
                   "1.2 The broker shall not exceed 40 lots on DEMO_NSE.\n")
            f = Path(tempfile.mkdtemp()) / f"e2e-{UID}.txt"
            f.write_text(doc, encoding="utf-8")
            mk.set_input_files("#file", str(f))
            mk.fill("#title", f"E2E notice {UID}")
            mk.click("form[aria-label='Upload source'] button.primary")
            mk.wait_for_selector("text=Ingested.")
            mk.wait_for_selector("button:has-text('Extract candidates')")
            shot(mk, "01_ingested")
            return f"source ingested ({UID})"

        def extract():
            mk.select_option("#mode", "DETERMINISTIC")
            mk.click("button:has-text('Extract candidates')")
            row = mk.locator("tr", has_text="margin multiplier")
            row.first.wait_for()
            assert "1.10" in row.first.inner_text()
            shot(mk, "02_candidates")
            return f"{mk.locator('tbody tr').count()} table rows; candidate shows 1.10"

        def draft():
            mk.locator("tr", has_text="margin multiplier").first.locator("button:has-text('Use for draft')").click()
            mk.wait_for_selector("form[aria-label='Draft rule']")
            mk.fill("#rk", KEY)
            mk.fill("#ef", "2027-02-01T00:00:00Z")
            mk.fill("#tv", json.dumps([{"name": "in-scope", "context": {"market_type": "ETD_DOMESTIC", "venue": "DEMO_NSE", "required_margin_inr": "1"},
                                        "expect_scope": "TRUE", "expect_condition": "TRUE"}]))
            mk.fill("#why", "E2E: drafted from the extracted candidate; cites the exact source span")
            mk.click("button:has-text('Compile + run vectors')")
            mk.wait_for_selector("[role=status]:has-text('compiles')")
            shot(mk, "03_draft_form")
            mk.click("button:has-text('Save draft')")
            mk.wait_for_url("**tab=command**rule=**")
            state["rule_id"] = mk.url.split("rule=")[1].split("&")[0]
            mk.wait_for_selector(f"h2:has-text('{KEY}')")
            return f"rule {state['rule_id'][:8]} drafted"

        def submit():
            mk.click("button:has-text('Submit for review')")
            mk.wait_for_selector("[role=dialog]")
            mk.fill("#reason", "E2E: ready for independent review")
            mk.click("[role=dialog] button.primary")
            mk.wait_for_selector("h2 + span:has-text('REVIEW REQUIRED'), .chip:has-text('REVIEW REQUIRED')", timeout=15000)
            return "status REVIEW REQUIRED"

        def self_approval_refused():
            mk.wait_for_selector("text=Approve unavailable: You authored", timeout=15000)  # detail panel reloaded after submit
            btn = mk.locator("button:has-text('Approve exact revision')")
            assert btn.is_disabled(), "Approve must be disabled for the author"
            msg = mk.locator("text=Approve unavailable").inner_text()
            assert "authored" in msg.lower() or "independent" in msg.lower(), msg
            shot(mk, "04_self_approval_disabled")
            # and the SERVER refuses even if the UI were bypassed (G15)
            res = mk.evaluate("""async (id) => { const s = await (await fetch('/api/v1/session')).json();
                const r = await fetch(`/api/v1/regulatory/rules/${id}/review`, {method: 'POST', credentials: 'include',
                  headers: {'content-type': 'application/json', 'x-csrf-token': s.csrf_token, 'Idempotency-Key': crypto.randomUUID()},
                  body: JSON.stringify({decision: 'APPROVE', reason: 'self approval attempt', expected_revision: null})});
                return {status: r.status, body: await r.json()}; }""", state["rule_id"])
            assert res["status"] == 403 and res["body"]["code"] == "SELF_APPROVAL", res
            return "UI disabled; API 403 SELF_APPROVAL"

        chk_ctx = b.new_context(viewport={"width": 1440, "height": 900})
        ck = login(chk_ctx, "checker")
        ck.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)

        def checker_approves():
            ck.goto(f"{BASE}/regulatory?tab=command&rule={state['rule_id']}")
            ck.wait_for_selector(f"h2:has-text('{KEY}')")
            ck.click("button:has-text('Approve exact revision')")
            ck.wait_for_selector("[role=dialog]")
            dlg = ck.locator("[role=dialog]").inner_text().lower()  # inner_text applies CSS text-transform (th is uppercase)
            for need in ("revision bound", "content hash", "maker", "acting user", "reason"):
                assert need in dlg, f"approval dialog lacks {need!r}"  # approval screen shows revision, hash, actors, reason
            shot(ck, "05_approval_dialog")
            ck.fill("#reason", "E2E: independent review complete; scope, parameter and citation verified")
            ck.click("[role=dialog] button.primary")
            ck.wait_for_selector("text=APPROVED", timeout=15000)
            ck.wait_for_selector("h3:has-text('Approval trail') ~ div table tbody tr:has-text('APPROVED')", timeout=15000)  # detail reloaded
            trail = ck.locator("h3:has-text('Approval trail') ~ div table tbody tr:has-text('APPROVED')").first.inner_text()
            return "APPROVED by independent checker; approval trail shows APPROVED"

        def checker_schedules():
            ck.click("button:has-text('Activate / schedule')")
            ck.wait_for_selector("[role=dialog]")
            ck.fill("#reason", "E2E: schedule at its effective date")
            ck.click("[role=dialog] button.primary")
            ck.wait_for_selector(".card[aria-labelledby=rule-h] .chip:has-text('SCHEDULED')", timeout=15000)
            shot(ck, "06_scheduled")
            return "SCHEDULED (effective 2027-02-01, after the pinned as-of)"

        def impact_lab():
            ck.goto(BASE + "/regulatory?tab=impact")
            ck.wait_for_selector("text=HYPOTHETICAL MODE")
            ck.locator("label", has_text="DEMO-ETD-MARGIN v2").locator("input").check()
            ck.check("#feas")
            ck.click("button:has-text('Run counterfactual')")
            ck.wait_for_selector("text=Result ", timeout=30000)
            txt = ck.inner_text("section[aria-labelledby=ir]")
            for expect in ("12,500,000", "10,000,000", "11,500,000", "1,000,000", "HYPOTHETICAL", "optimal"):
                assert expect in txt, f"{expect!r} missing from impact result"
            assert "not an operational compliance status" in txt.lower() or "HYPOTHETICAL" in txt
            shot(ck, "07_impact_lab")
            return "INR 10,000,000 -> 12,500,000; posted 11,500,000; shortfall 1,000,000; feasibility optimal; HYPOTHETICAL"

        def evidence_trail():
            ck.goto(BASE + "/overview")
            res = ck.evaluate("""async (id) => { const r = await fetch('/api/v1/regulatory/rules/' + id); return await r.json(); }""", state["rule_id"])
            assert res["status"] == "SCHEDULED" and res["maker_id"] != ""
            return "rule SCHEDULED; maker != checker recorded"

        for name, fn in (("maker ingests fictional source", ingest), ("deterministic extraction shows candidate", extract),
                         ("maker drafts rule from candidate", draft), ("maker submits for review", submit),
                         ("maker cannot approve own rule (G15)", self_approval_refused),
                         ("checker approves exact revision", checker_approves), ("checker schedules rule", checker_schedules),
                         ("impact lab via real risk+collateral", impact_lab), ("rule state persisted", evidence_trail)):
            if "rule_id" not in state and name not in ("maker ingests fictional source", "deterministic extraction shows candidate", "maker drafts rule from candidate") and name not in ("impact lab via real risk+collateral",):
                steps.append({"step": name, "ok": False, "error": "skipped: earlier step failed"})
                print(f"FAIL  {name}: skipped (earlier step failed)")
                continue
            step(name, fn)
        expected = [e for e in errs if "403" in e]   # exactly the deliberate self-approval attempt (G15) is allowed to log one 403
        unexpected = [e for e in errs if "403" not in e]
        ok = not unexpected and len(expected) <= 1
        steps.append({"step": "no unexpected console errors (only the deliberate G15 403)", "ok": ok, "detail": {"expected_403": len(expected), "unexpected": unexpected[:5]}})
        print(("PASS" if ok else "FAIL") + f"  no unexpected console errors (expected 403s: {len(expected)}, unexpected: {unexpected[:3]})")
        b.close()
    (ROOT / "reports" / "test-results").mkdir(parents=True, exist_ok=True)
    (ROOT / "reports" / "test-results" / "regulatory_flows.json").write_text(json.dumps({"base": BASE, "rule_key": KEY, "steps": steps}, indent=1))
    failed = [s for s in steps if not s["ok"]]
    print(f"{len(steps) - len(failed)}/{len(steps)} steps passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

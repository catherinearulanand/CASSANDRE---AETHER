"""ATLAS browser flows (Playwright + system Edge) against a RUNNING stack prepared by tests/e2e/collateral_setup.py.

    python tests/e2e/collateral_flows.py [base_url]        (default http://localhost:3104)

Every business number asserted here is either an independent hand value from the spec (G01: 1,000,000 INR gap; G03: 9,500,000 USD
call) or re-derived in this script with Decimal from the API's own persisted legs (coverage >= required, legs within inventory).
Results -> reports/test-results/collateral_flows.json ; screenshots -> reports/screenshots/collateral_*.png
Exit code 1 if any check fails.
"""
import json
import re
import sys
import uuid
from decimal import Decimal
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:3104"
CREDS = json.loads((ROOT / "var" / "a4-creds.json").read_text())["tenant-a"]
IDS = json.loads((ROOT / "var" / "a4-ids.json").read_text())
SHOTS = ROOT / "reports" / "screenshots"
SHOTS.mkdir(parents=True, exist_ok=True)
RESULTS: list[dict] = []
D = Decimal


def check(name, cond, detail=""):
    RESULTS.append({"check": name, "ok": bool(cond), "detail": str(detail)[:300]})
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    return bool(cond)


def alloc_url(margin, snap, tab="allocation", proposal=None):
    u = f"{BASE}/collateral?tab={tab}&margin={margin}&snap={snap}&deadline={IDS['deadline']}Z&h=30"
    return u + (f"&proposal={proposal}" if proposal else "")


def login(browser, user, viewport=None):
    ctx = browser.new_context(viewport=viewport or {"width": 1440, "height": 900}, accept_downloads=True)
    page = ctx.new_page()
    page.goto(BASE + "/overview")
    page.fill("#username", user)
    page.fill("#password", CREDS[user])
    page.click("button.primary")
    page.wait_for_selector(".shell")
    return ctx, page


def csrf(page):
    return page.request.get(BASE + "/api/v1/session").json()["csrf_token"]


def api_post(page, path, body, key=None):
    return page.request.post(BASE + "/api/v1" + path, data=json.dumps(body),
                             headers={"content-type": "application/json", "x-csrf-token": csrf(page),
                                      "Idempotency-Key": key or str(uuid.uuid4())})


def api_get(page, path):
    return page.request.get(BASE + "/api/v1" + path).json()


def run_optimisation(page, margin, snap):
    page.goto(alloc_url(margin, snap))
    page.get_by_role("button", name="Run optimisation").click()
    page.wait_for_url(re.compile(r"proposal="), timeout=30000)
    page.wait_for_selector("text=/solver:/", timeout=30000)
    return re.search(r"proposal=([0-9a-f-]{36})", page.url).group(1)


def state_chip(page):
    return page.locator(".chip").filter(has_text=re.compile(r"^.\s*(PROPOSED|CHECKED|APPROVED|RESERVED|DRAFTED|INFEASIBLE|EXPIRED|CANCELLED|NOT REQUIRED|HYPOTHETICAL)$")).first


def approval_tab(page, pid, margin=IDS["otc_post"], snap=IDS["otc_snapshot"]):
    page.goto(alloc_url(margin, snap, "approval", pid))
    page.wait_for_selector("h2:has-text('Maker–checker controls')", timeout=20000)


def confirm(page, label, reason=None):
    dlg = page.get_by_role("dialog")
    expect(dlg).to_be_visible()
    if reason:
        dlg.locator("#cd-reason").fill(reason)
    dlg.get_by_role("button", name=label, exact=True).click()


def main():
    with sync_playwright() as p:
        b = p.chromium.launch(channel="msedge", headless=True)

        # ------------------------------------------------------------------ F1: HYPOTHETICAL G01 (ETD) is flagged and never approvable
        mk_ctx, mk = login(b, "maker")
        pid_h = run_optimisation(mk, IDS["etd_hyp"], IDS["etd_snapshot"])
        body = mk.inner_text("main")
        check("F1 hypothetical G01: UI shows HYPOTHETICAL marker", "HYPOTHETICAL" in body)
        check("F1 hypothetical G01: required coverage is exactly 1,000,000 INR (12.5m - 11.5m posted)", "1,000,000" in body, body[:200])
        mk.screenshot(path=str(SHOTS / "collateral_g01_hypothetical_allocation.png"), full_page=True)
        ck_ctx, ck = login(b, "checker")
        approval_tab(ck, pid_h, IDS["etd_hyp"], IDS["etd_snapshot"])
        btn = ck.get_by_role("button", name=re.compile(r"^Approve"))
        check("F1 approve button disabled for hypothetical proposal", btn.is_disabled())
        check("F1 UI explains why (note + tooltip)", "HYPOTHETICAL" in ck.inner_text("main") and "HYPOTHETICAL" in (btn.get_attribute("title") or ""))
        r = api_post(ck, f"/collateral/proposals/{pid_h}/approve", {"reason": "try", "expected_revision": None})
        check("F1 server refuses approval of hypothetical proposal (403 NOT_APPROVABLE)", r.status == 403 and r.json()["code"] == "NOT_APPROVABLE", r.text())
        ck.screenshot(path=str(SHOTS / "collateral_g01_hypothetical_not_approvable.png"), full_page=True)

        # ------------------------------------------------------------------ F2: real OTC G03 (9,500,000 USD) maker -> checker -> reserve -> draft -> simulated
        pid_a = run_optimisation(mk, IDS["otc_post"], IDS["otc_snapshot"])
        body = mk.inner_text("main")
        check("F2 OTC G03: required coverage 9,500,000 USD", "9,500,000" in body, body[:200])
        check("F2 OTC: solver optimal + CHECKED + NOT TRANSMITTED visible", "solver: optimal" in body and "CHECKED" in body and "NOT TRANSMITTED" in body)
        check("F2 OTC: baseline vs recommended shown (feasible case)", "Baseline vs recommended" in body)
        mk.screenshot(path=str(SHOTS / "collateral_g03_allocation.png"), full_page=True)
        prop = api_get(mk, f"/collateral/proposals/{pid_a}")
        cov = sum(D(l["effective_coverage"]) for l in prop["legs"])
        check("F2 independent re-check: sum(effective coverage of legs) >= required", cov >= D(prop["required_coverage"]) == D("9500000"), f"{cov}")
        inv = {l["lot_id"]: D(l["available"]) for l in api_get(mk, f"/collateral/inventory/{IDS['otc_snapshot']}")["lots"]}
        check("F2 independent re-check: every leg within inventory", all(D(l["quantity"]) <= inv[l["lot_id"]] for l in prop["legs"]))
        check("F2 wrong-way lot never allocated", "CORP-USD-WW" not in [l["lot_id"] for l in prop["legs"]])

        # maker cannot approve own work: compliance officer holds BOTH roles -> only the 'selfMaker' rule can stop them
        cp_ctx, cp = login(b, "compliance")
        pid_c = run_optimisation(cp, IDS["otc_post"], IDS["otc_snapshot"])
        approval_tab(cp, pid_c)
        btn = cp.get_by_role("button", name=re.compile(r"^Approve"))
        check("F2 maker (also a checker role) sees Approve disabled on own proposal", btn.is_disabled() and "made this proposal" in cp.inner_text("main"))
        r = api_post(cp, f"/collateral/proposals/{pid_c}/approve", {"reason": "self", "expected_revision": None})
        check("F2 server refuses self-approval (403 SELF_APPROVAL)", r.status == 403 and r.json()["code"] == "SELF_APPROVAL", r.text())
        cp.screenshot(path=str(SHOTS / "collateral_self_approval_refused.png"), full_page=True)

        # checker approves with a stale second browser context -> conflict message (G13-style optimistic lock)
        pid_b = run_optimisation(mk, IDS["otc_post"], IDS["otc_snapshot"])   # competing proposal for the same inventory
        ck2_ctx, ck2 = login(b, "checker")
        approval_tab(ck, pid_a)
        approval_tab(ck2, pid_a)                                              # both contexts now hold the CHECKED revision
        ck.get_by_role("button", name=re.compile(r"^Approve")).click()
        confirm(ck, "Approve", "independent review: constraints and hashes checked")
        ck.wait_for_selector("text=/APPROVED/", timeout=20000)
        check("F2 checker approval succeeds and state becomes APPROVED", "APPROVED" in ck.inner_text("main"))
        ck2.get_by_role("button", name=re.compile(r"^Approve")).click()       # stale view still offers Approve
        confirm(ck2, "Approve", "second checker, stale view")
        ck2.wait_for_selector("[role=dialog] [role=alert]", timeout=20000)
        msg = ck2.locator("[role=dialog] [role=alert]").inner_text()
        check("F2 second (stale) approval gets an explicit conflict message", re.search(r"REVISION_CONFLICT|ILLEGAL_TRANSITION", msg) is not None, msg)
        ck2.screenshot(path=str(SHOTS / "collateral_stale_second_approval_conflict.png"), full_page=True)
        ck2.keyboard.press("Escape")
        ck2.wait_for_selector("text=/APPROVED/", timeout=20000)               # UI reloaded the latest revision
        check("F2 conflicted view reloads to the latest revision (APPROVED)", "APPROVED" in ck2.inner_text("main"))
        rows = api_get(cp, f"/evidence/events?entity_id={pid_a}&limit=100")["items"]
        check("F2 exactly one COLLATERAL_PROPOSAL_APPROVED evidence event", sum(1 for e in rows if e["action"] == "COLLATERAL_PROPOSAL_APPROVED") == 1,
              [e["action"] for e in rows])

        # approve competing proposal, reserve first, second must fail (no double pledge)
        approval_tab(ck, pid_b)
        ck.get_by_role("button", name=re.compile(r"^Approve")).click()
        confirm(ck, "Approve", "competing proposal approved by independent checker")
        ck.wait_for_selector("text=/APPROVED/", timeout=20000)
        approval_tab(mk, pid_a)
        mk.get_by_role("button", name=re.compile(r"^Reserve")).click()
        confirm(mk, "Reserve")
        mk.wait_for_selector("text=/RESERVED/", timeout=20000)
        check("F2 reservation succeeds -> RESERVED (reserved is not posted)", "RESERVED" in mk.inner_text("main"))
        approval_tab(mk, pid_b)
        mk.get_by_role("button", name=re.compile(r"^Reserve")).click()
        confirm(mk, "Reserve")
        mk.wait_for_selector("[role=dialog] [role=alert]", timeout=20000)
        msg = mk.locator("[role=dialog] [role=alert]").inner_text()
        check("F2 competing reservation refused (STALE_INVENTORY/RESERVATION_CONFLICT), nothing double-pledged",
              re.search(r"STALE_INVENTORY|RESERVATION_CONFLICT", msg) is not None, msg)
        mk.screenshot(path=str(SHOTS / "collateral_competing_reservation_refused.png"), full_page=True)
        mk.keyboard.press("Escape")
        inv2 = {l["lot_id"]: l for l in api_get(mk, f"/collateral/inventory/{IDS['otc_snapshot']}")["lots"]}
        check("F2 no lot is over-reserved", all(D(l["reserved"]) + D(l["encumbered"]) <= D(l["units"]) for l in inv2.values()))

        # draft + NOT TRANSMITTED + download + simulated settlement
        approval_tab(mk, pid_a)
        mk.get_by_role("button", name=re.compile(r"^Draft instruction")).click()
        confirm(mk, "Create draft")
        mk.wait_for_selector("text=/SYNTHETIC DRAFT - NOT TRANSMITTED/", timeout=20000)
        check("F2 draft shows NOT TRANSMITTED watermark", "NOT TRANSMITTED" in mk.inner_text("main"))
        with mk.expect_download() as dl:
            mk.get_by_role("button", name=re.compile(r"^Download watermarked draft")).click()
        d = dl.value
        content = json.loads(Path(d.path()).read_text())
        check("F2 downloaded draft file is watermarked + NOT_TRANSMITTED", "NOT_TRANSMITTED" in d.suggested_filename and
              content["content"]["status"] == "NOT_TRANSMITTED" and "NOT TRANSMITTED" in content["content"]["watermark"], d.suggested_filename)
        mk.screenshot(path=str(SHOTS / "collateral_draft_not_transmitted.png"), full_page=True)
        mk.get_by_role("button", name=re.compile(r"^Record SIMULATED settlement")).click()
        confirm(mk, "Record simulation")
        mk.wait_for_selector("text=/SIMULATED - no real movement occurred/", timeout=20000)
        check("F2 simulated settlement is clearly labelled SIMULATED", "SIMULATED - no real movement occurred" in mk.inner_text("main"))
        mk.screenshot(path=str(SHOTS / "collateral_simulated_settlement.png"), full_page=True)
        acts = [e["action"] for e in sorted(api_get(cp, f"/evidence/events?correlation_id={prop['correlation_id']}&limit=200")["items"], key=lambda e: e["sequence"])]
        want = ["COLLATERAL_PROPOSAL_CREATED", "COLLATERAL_PROPOSAL_CHECKED", "COLLATERAL_PROPOSAL_APPROVED", "COLLATERAL_APPROVAL_RECORDED",
                "COLLATERAL_RESERVED", "COLLATERAL_INSTRUCTION_DRAFTED", "COLLATERAL_SIMULATED_SETTLED"]
        it = iter(acts)
        check("F2 evidence trail contains every step in order", all(any(a == w for a in it) for w in want), acts)

        # ------------------------------------------------------------------ F3: G07 infeasible -> no savings card, no approval, no draft
        from app.engines.collateral.fixtures import otc as otc_fixture
        small = otc_fixture()
        small["snapshot_id"] = "INV-G07-" + uuid.uuid4().hex[:6]
        small["lots"] = [dict(small["lots"][0], lot_id="CASH-USD-SMALL", units="3000000")]
        r = api_post(mk, "/collateral/inventory/import", small)
        check("F3 setup: small inventory imported", r.status == 201, r.text())
        pid_i = run_optimisation(mk, IDS["otc_post"], small["snapshot_id"])
        body = mk.inner_text("main")
        check("F3 G07 infeasible: UI states INFEASIBLE with the shortfall", "INFEASIBLE" in body and "no allocation" in body.lower())
        check("F3 no savings/baseline comparison card for an infeasible case", "Baseline vs recommended" not in body and "savings" not in body.lower().replace("modelled horizon cost, not realised savings", ""))
        mk.screenshot(path=str(SHOTS / "collateral_g07_infeasible.png"), full_page=True)
        approval_tab(ck, pid_i, IDS["otc_post"], small["snapshot_id"])
        check("F3 infeasible: approve disabled and no instruction draft offered",
              ck.get_by_role("button", name=re.compile(r"^Approve")).is_disabled() and "No instruction draft" in ck.inner_text("main"))
        r = api_post(ck, f"/collateral/proposals/{pid_i}/draft", {"expected_revision": None})
        check("F3 server refuses a draft for an infeasible proposal (409)", r.status == 409, r.text())

        # ------------------------------------------------------------------ F4: zero call
        mk.goto(alloc_url(IDS["otc_zero"], IDS["otc_snapshot"]))
        mk.get_by_role("button", name="Run optimisation").click()
        mk.wait_for_selector("text=/No additional collateral required/", timeout=30000)
        check("F4 zero call: no allocation, explicit 'No additional collateral required'", "No additional collateral required" in mk.inner_text("main"))

        # ------------------------------------------------------------------ F5: stress lab is hypothetical and moves nothing
        before = api_get(mk, f"/collateral/inventory/{IDS['otc_snapshot']}")["lots"]
        mk.goto(alloc_url(IDS["otc_post"], IDS["otc_snapshot"], "stress"))
        mk.wait_for_selector("h2, h3", timeout=20000)
        mk.screenshot(path=str(SHOTS / "collateral_stress_lab_before.png"), full_page=True)
        mk.fill("#st-h", "5")
        mk.get_by_role("button", name="Run stress").click()
        mk.wait_for_selector("text=/HYPOTHETICAL/", timeout=30000)
        check("F5 stress lab result is labelled HYPOTHETICAL", "HYPOTHETICAL" in mk.inner_text("main"))
        after = api_get(mk, f"/collateral/inventory/{IDS['otc_snapshot']}")["lots"]
        check("F5 stress never moves or reserves assets", [(l["lot_id"], l["reserved"], l["units"]) for l in before] ==
              [(l["lot_id"], l["reserved"], l["units"]) for l in after])
        mk.screenshot(path=str(SHOTS / "collateral_stress_lab_after.png"), full_page=True)

        # ------------------------------------------------------------------ F6: role gating + tenant isolation
        vw_ctx, vw = login(b, "viewer")
        vw.goto(alloc_url(IDS["otc_post"], IDS["otc_snapshot"]))
        check("F6 viewer cannot run optimisation (button disabled)", vw.get_by_role("button", name="Run optimisation").is_disabled())
        r = api_post(vw, "/collateral/optimise", {"margin_result_id": IDS["otc_post"], "inventory_snapshot_id": IDS["otc_snapshot"],
                                                   "deadline": IDS["deadline"] + ":00Z", "horizon_days": 30})
        check("F6 server refuses viewer optimise (403)", r.status == 403, r.status)
        ctxb = b.new_context()
        pg = ctxb.new_page()
        pg.goto(BASE + "/overview")
        creds_b = json.loads((ROOT / "var" / "a4-creds.json").read_text()).get("tenant-b", {})
        pg.fill("#tenant", "tenant-b")
        pg.fill("#username", "maker")
        pg.fill("#password", creds_b["maker"])
        pg.click("button.primary")
        pg.wait_for_selector(".shell")
        r = pg.request.get(f"{BASE}/api/v1/collateral/proposals/{pid_a}")
        check("F6 tenant B gets 404 for tenant A's proposal (no leakage)", r.status == 404, r.status)
        r = pg.request.get(f"{BASE}/api/v1/collateral/proposals/{pid_a}/instruction")
        check("F6 tenant B gets 404 for tenant A's instruction draft", r.status == 404, r.status)
        ctxb.close()

        for c in (mk_ctx, ck_ctx, ck2_ctx, cp_ctx, vw_ctx):
            c.close()
        b.close()

    bad = [r for r in RESULTS if not r["ok"]]
    out = ROOT / "reports" / "test-results"
    out.mkdir(parents=True, exist_ok=True)
    (out / "collateral_flows.json").write_text(json.dumps({"base": BASE, "checks": RESULTS, "passed": len(RESULTS) - len(bad), "failed": len(bad)}, indent=1))
    print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} checks passed")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()

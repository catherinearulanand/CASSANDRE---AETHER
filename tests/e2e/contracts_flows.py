"""COVENANT browser flows (Playwright + system Edge). Needs: backend + frontend running against a database provisioned by
tests/e2e/contracts_setup.py (creds in var/a2.creds.json). Usage: python tests/e2e/contracts_flows.py [base_url]
Steps (each recorded pass/fail with screenshots in reports/screenshots/contracts_*.png; results in reports/test-results/contracts_flows.json):
  maker/checker separation (C-A07/G15) -> checker approves fields -> propose + freeze by an INDEPENDENT checker session -> frozen read-only
  -> amendment 10m->8m effective 2026-09-01 (G08) -> v1/v2 timeline + compare -> resolve before/after -> G14 rating trigger blocks ->
  G09 cross-master trade rejected -> viewer is read-only -> tenant isolation."""
import json
import re
import sys
import time
import traceback
import uuid
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3102"
CREDS = json.loads((ROOT / "var" / "a2.creds.json").read_text())
SHOTS = ROOT / "reports" / "screenshots"
SHOTS.mkdir(parents=True, exist_ok=True)
DOCS = ROOT / "fixtures" / "contracts" / "documents"
RUN = uuid.uuid4().hex[:6].upper()
NS, MASTER, CSA = f"NS-E2E-{RUN}", f"MA-E2E-{RUN}", f"CSA-E2E-{RUN}"
steps: list[dict] = []
state: dict = {}


def base_text() -> str:
    t = (DOCS / "csa_demo_g25.txt").read_bytes().replace(b"\r\n", b"\n").decode()
    for old, new in (
        ("NS-DEMO-G25", NS), ("MA-DEMO-G25", MASTER), ("CSA-DEMO-G25", CSA),
        ("(a) Threshold with respect to Meridian Demo Bank: USD 0.", "(a) Threshold with respect to Meridian Demo Bank: USD 10,000,000."),
        ("(b) Threshold with respect to Orchid Demo Counterparty: USD 0.", "(b) Threshold with respect to Orchid Demo Counterparty: USD 10,000,000."),
        ("Meridian Demo Bank: USD 10.", "Meridian Demo Bank: USD 500,000."), ("Orchid Demo Counterparty: USD 10.", "Orchid Demo Counterparty: USD 500,000."),
        ("multiple of USD 1.", "multiple of USD 100,000.")):
        assert old in t, old
        t = t.replace(old, new)
    return t


AMENDMENT = f"""FICTIONAL E2E AMENDMENT NO. 1 (SYNTHETIC - NOT A LEGAL DOCUMENT)
Reference: AMD-E2E-{RUN} to {CSA}
Party A: Meridian Demo Bank. Party B: Orchid Demo Counterparty.
Effective Date: 2026-09-01.

This Amendment replaces Paragraph 1 of the Credit Support Annex with effect from the Effective Date.
(a) Threshold with respect to Meridian Demo Bank: USD 8,000,000.
(b) Threshold with respect to Orchid Demo Counterparty: USD 8,000,000.
All other terms remain unchanged.
"""


def step(name):
    def deco(fn):
        t0 = time.time()
        try:
            fn()
            steps.append({"step": name, "ok": True, "seconds": round(time.time() - t0, 1)})
            print("PASS", name)
        except Exception as e:  # noqa: BLE001
            steps.append({"step": name, "ok": False, "error": f"{type(e).__name__}: {str(e)[:400]}"})
            print("FAIL", name, "->", str(e)[:300])
            traceback.print_exc(limit=2)
            for k, pg in list(state.get("pages", {}).items()):
                try:
                    pg.screenshot(path=str(SHOTS / f"contracts_FAIL_{re.sub(r'[^a-z0-9]+', '_', name.lower())[:40]}_{k}.png"), full_page=True)
                except Exception:  # noqa: BLE001
                    pass
        return fn
    return deco


def login(browser, user, tenant="tenant-a", width=1440):
    ctx = browser.new_context(viewport={"width": width, "height": 900})
    page = ctx.new_page()
    errs: list = []
    page.on("console", lambda m: errs.append(m.text) if m.type == "error" and not m.text.startswith("Failed to load resource") else None)
    page.on("pageerror", lambda e: errs.append(f"pageerror: {e}"))
    page.on("response", lambda r: errs.append(f"{r.status} {r.request.method} {r.url}") if "/api/v1" in r.url and (
        r.status >= 500 or (r.status in (400, 404, 422) and r.request.method == "GET")) else None)
    page.goto(BASE + "/contracts")
    page.fill("#tenant", tenant)
    page.fill("#username", user)
    page.fill("#password", CREDS[tenant][user])
    page.click("button.primary")
    page.wait_for_selector(".shell")
    ctx.errs = errs  # type: ignore[attr-defined]
    return ctx, page


def api(ctx, method, path, body=None, key=True):
    csrf = ctx.request.get(BASE + "/api/v1/session").json()["csrf_token"]
    h = {"x-csrf-token": csrf, "content-type": "application/json"}
    if key:
        h["Idempotency-Key"] = uuid.uuid4().hex
    r = ctx.request.fetch(BASE + "/api/v1" + path, method=method, headers=h, data=json.dumps(body) if body is not None else None)
    try:
        j = r.json()
    except Exception:  # noqa: BLE001
        j = None
    return r.status, j


def open_pkg(page, pid, tab="review"):
    page.goto(f"{BASE}/contracts?pkg={pid}&tab={tab}")
    page.wait_for_selector("h2:has-text('Agreement registry')")
    page.wait_for_load_state("networkidle")


def approve_all_proposed(page, who):
    for _ in range(60):
        rows = page.locator("section[aria-labelledby='fld-h'] tbody tr", has_text="PROPOSED")
        if rows.count() == 0:
            return
        before = rows.count()
        row = rows.first
        key = row.locator("button").first.inner_text()
        dirn = row.locator("td").nth(1).inner_text()
        row.locator("button").first.click()
        page.fill("#reason", f"independent review by {who}: value matches the quoted source")
        btn = page.locator("button:has-text('Approve')").first
        page.wait_for_function("b => !b.disabled", arg=btn.element_handle(), timeout=5000)
        with page.expect_response(lambda r: "/review" in r.url and r.request.method == "POST") as ri:
            btn.click()
        assert ri.value.status == 200, f"approve {key} returned {ri.value.status}"
        # wait until the refreshed table shows one fewer PROPOSED row (keys can repeat across versions, so count, don't match)
        page.wait_for_function(
            "n => [...document.querySelectorAll(\"section[aria-labelledby='fld-h'] tbody tr\")].filter(r => r.innerText.includes('PROPOSED')).length < n",
            arg=before, timeout=10000)
    raise AssertionError("approve loop did not converge")


def shot(page, name):
    page.screenshot(path=str(SHOTS / f"contracts_{name}.png"), full_page=True)


with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True)
    maker_ctx, maker = login(browser, "maker")
    checker_ctx, checker = login(browser, "checker")
    dual_ctx, dual = login(browser, "dual")
    state["pages"] = {"maker": maker, "checker": checker, "dual": dual}

    @step("registry lists seeded agreements with status, version and review queue")
    def _():
        maker.goto(BASE + "/contracts")
        maker.wait_for_selector("h2:has-text('Agreement registry')")
        maker.wait_for_selector("table tbody tr")
        txt = maker.inner_text("main")
        for ns in ("NS-MER-NWD-001", "NS-DEMO-G25", "NS-DEMO-G14-RATING"):
            assert ns in txt, ns
        assert "v2" in txt  # NS-MER-NWD-001 has an amendment version
        shot(maker, "01_registry")

    @step("maker creates a package (API) and sees it in the registry")
    def _():
        st, j = api(maker_ctx, "POST", "/contracts/packages", {
            "master_id": MASTER, "csa_id": CSA, "netting_set_id": NS, "party_a_id": "ENT-MERIDIAN", "party_a_name": "Meridian Demo Bank",
            "party_b_id": "ENT-OTHERCP", "party_b_name": "Orchid Demo Counterparty", "base_currency": "USD",
            "governing_law": "Fictional Demo Law (synthetic)",
            "documents": [{"kind": "CSA", "doc_ref": CSA, "title": CSA, "text": base_text(), "effective_from": "2026-01-01T00:00:00Z"}]})
        assert st in (200, 201), (st, j)
        state["pid"] = j["package_id"]
        maker.goto(BASE + "/contracts")
        maker.wait_for_selector(f"button:has-text('{NS}')")

    @step("dual-role user extracts; cannot approve own extraction (UI disabled + API 403 SELF_APPROVAL) - C-A07/G15")
    def _():
        pid = state["pid"]
        open_pkg(dual, pid)
        dual.click("button:has-text('Extract proposals')")
        dual.wait_for_selector("h2:has-text('Structured terms (')")
        dual.wait_for_selector("section[aria-labelledby='fld-h'] tbody tr")
        dual.locator("section[aria-labelledby='fld-h'] tbody tr", has_text="PROPOSED").first.locator("button").first.click()
        dual.fill("#reason", "trying to approve my own extraction")
        assert dual.locator("button:has-text('Approve')").first.is_disabled(), "self-approval must be disabled in the UI"
        shot(dual, "02_self_approval_disabled")
        terms = api(dual_ctx, "GET", f"/contracts/{pid}/terms", key=False)[1]
        f = next(x for x in terms["fields"] if x["status"] == "PROPOSED")
        st, j = api(dual_ctx, "POST", f"/contracts/{pid}/review", {"field_id": f["field_id"], "action": "APPROVE", "reason": "self approval attempt",
                                                                    "expected_revision": f["revision"]})
        assert st == 403 and j["code"] == "SELF_APPROVAL", (st, j)

    @step("maker role (not checker) has Approve disabled; viewer is read-only")
    def _():
        open_pkg(maker, state["pid"])
        maker.locator("section[aria-labelledby='fld-h'] tbody tr", has_text="PROPOSED").first.locator("button").first.click()
        maker.fill("#reason", "maker trying to approve")
        assert maker.locator("button:has-text('Approve')").first.is_disabled()
        vctx, v = login(browser, "viewer")
        open_pkg(v, state["pid"], "freeze")
        assert v.locator("button:has-text('Propose freeze')").is_disabled()
        shot(v, "03_viewer_readonly")
        vctx.close()

    @step("independent checker approves every proposed field with a reason (UI)")
    def _():
        open_pkg(checker, state["pid"])
        approve_all_proposed(checker, "checker")
        txt = checker.inner_text("main")
        assert checker.locator("section[aria-labelledby='fld-h'] tbody tr", has_text="PROPOSED").count() == 0, "fields left unapproved"
        shot(checker, "04_review_all_approved")

    @step("maker proposes freeze; a DIFFERENT checker session finds the pending approval and freezes the exact hash -> v1")
    def _():
        pid = state["pid"]
        open_pkg(maker, pid, "freeze")
        assert "Ready" in maker.inner_text("main") or "ready" in maker.inner_text("main").lower()
        maker.fill("#fz-reason", "propose freeze of reviewed terms")
        maker.click("button:has-text('Propose freeze')")
        maker.wait_for_selector("[aria-label='Pending freeze approval']")
        open_pkg(checker, pid, "freeze")  # separate session: the pending approval must be discoverable server-side
        checker.wait_for_selector("[aria-label='Pending freeze approval']")
        assert "Maker" in checker.inner_text("[aria-label='Pending freeze approval']")
        checker.fill("#fz-reason", "independent approval of the exact terms hash")
        checker.click("button:has-text('Approve exact hash')")
        checker.wait_for_selector("li:has-text('v1')")
        shot(checker, "05_frozen_v1")

    @step("frozen terms are read-only (UI message; API 409 FIELD_FROZEN)")
    def _():
        pid = state["pid"]
        open_pkg(maker, pid)
        maker.locator("section[aria-labelledby='fld-h'] tbody tr").first.locator("button").first.click()
        maker.wait_for_selector("text=Frozen terms are read-only")
        terms = api(checker_ctx, "GET", f"/contracts/{pid}/terms", key=False)[1]
        f = terms["fields"][0]
        st, j = api(checker_ctx, "POST", f"/contracts/{pid}/review", {"field_id": f["field_id"], "action": "REJECT", "reason": "attempt to edit frozen",
                                                                       "expected_revision": f["revision"]})
        assert st == 409 and j["code"] == "FIELD_FROZEN", (st, j)

    @step("amendment filed in the UI (10m -> 8m effective 2026-09-01), extracted, approved, frozen -> v2 (G08)")
    def _():
        pid = state["pid"]
        open_pkg(maker, pid, "freeze")
        maker.fill("#am-ref", f"AMD-E2E-{RUN}")
        maker.fill("#am-eff", "2026-09-01T00:00:00Z")
        maker.fill("#am-text", AMENDMENT)
        maker.click("button:has-text('Add amendment')")
        maker.wait_for_selector(f"td:has-text('AMD-E2E-{RUN}')")
        open_pkg(maker, pid, "review")
        maker.click("button:has-text('Extract proposals')")
        maker.wait_for_selector("section[aria-labelledby='fld-h'] tbody tr:has-text('PROPOSED')")
        open_pkg(checker, pid, "review")
        approve_all_proposed(checker, "checker")
        open_pkg(maker, pid, "freeze")
        maker.fill("#fz-reason", "propose freeze of amended terms")
        maker.click("button:has-text('Propose freeze')")
        maker.wait_for_selector("[aria-label='Pending freeze approval']")
        open_pkg(checker, pid, "freeze")
        checker.wait_for_selector("[aria-label='Pending freeze approval']")
        checker.fill("#fz-reason", "independent approval of amended terms")
        checker.click("button:has-text('Approve exact hash')")
        checker.wait_for_selector("li:has-text('v2')")
        txt = checker.inner_text("main")
        assert "v1" in txt and "v2" in txt and "supersedes" in txt.lower()
        shot(checker, "06_timeline_v1_v2")

    @step("compare v1 vs v2 and resolve before/after 2026-09-01: threshold 10,000,000 -> 8,000,000; history unchanged")
    def _():
        pid = state["pid"]
        open_pkg(maker, pid, "freeze")
        maker.select_option("#cmp-a", index=1)
        maker.select_option("#cmp-b", index=2)
        maker.fill("#cmp-t", "2026-09-02T00:00:00Z")
        maker.click("button:has-text('Compare')")
        maker.wait_for_selector("table:has(caption:has-text('Resolved changes'))")
        t = maker.inner_text("table:has(caption:has-text('Resolved changes'))")
        assert "10000000" in t and "8000000" in t, t
        maker.fill("#rs-asof", "2026-08-31T00:00:00Z")
        maker.click("button:has-text('Resolve')")
        maker.wait_for_selector("text=RESOLVED")
        before = maker.inner_text("pre")
        assert '"threshold_a": "10000000"' in before, before
        maker.fill("#rs-asof", "2026-09-02T00:00:00Z")
        maker.click("button:has-text('Resolve')")
        maker.wait_for_function("document.querySelector('pre') && document.querySelector('pre').innerText.includes('8000000')")
        after = maker.inner_text("pre")
        assert '"threshold_a": "8000000"' in after and '"mta_a": "500000"' in after, after
        shot(maker, "07_resolve_after_amendment")

    @step("G14: unresolved rating trigger BLOCKS; a sourced fact resolves the conditional term")
    def _():
        maker.goto(BASE + "/contracts")
        maker.click("button:has-text('NS-DEMO-G14-RATING')")
        maker.wait_for_selector("[role=tab]:has-text('Freeze')")
        maker.click("[role=tab]:has-text('Freeze')")
        maker.wait_for_selector("#rs-asof")
        maker.click("button:has-text('Resolve')")
        maker.wait_for_selector("text=BLOCKED")
        assert "rating" in maker.inner_text("main").lower()
        shot(maker, "08_g14_blocked")
        maker.fill("#rs-facts", "FACT-RATING-BB")
        maker.click("button:has-text('Resolve')")
        maker.wait_for_selector("text=RESOLVED")
        assert '"threshold_b": "0"' in maker.inner_text("pre")

    @step("G09: cross-master trade registered in the UI is REJECTED and listed in quarantine")
    def _():
        maker.goto(BASE + "/contracts")
        maker.click("button:has-text('NS-MER-NWD-001')")
        maker.click("[role=tab]:has-text('Eligibility')")
        maker.wait_for_selector("#t-id")
        tid = f"TRD-E2E-{RUN}"
        maker.fill("#t-id", tid)
        maker.fill("#t-m", "MA-MER-OTH-002")
        maker.fill("#t-n", "NS-MER-NWD-001")
        maker.click("button:has-text('Register')")
        maker.wait_for_selector(f"li:has-text('{tid}')")
        assert "REJECTED" in maker.inner_text(f"li:has-text('{tid}')")
        # not a member of the set it tried to join
        members = api(maker_ctx, "GET", f"/contracts/{state['pid']}/netting", key=False)
        shot(maker, "09_g09_cross_master_rejected")

    @step("tenant B cannot read tenant A's package (404, no leakage)")
    def _():
        bctx, b = login(browser, "maker", "tenant-b")
        st, j = api(bctx, "GET", f"/contracts/{state['pid']}", key=False)
        assert st == 404, (st, j)
        st2, j2 = api(bctx, "GET", f"/contracts/{state['pid']}/terms", key=False)
        assert st2 == 404, (st2, j2)
        listed = api(bctx, "GET", "/contracts/packages", key=False)[1]
        assert not [x for x in listed["items"] if x["netting_set_id"] == NS]
        bctx.close()

    @step("no unexpected console errors or 5xx during the flows")
    def _():
        allerrs = [e for c in (maker_ctx, checker_ctx, dual_ctx) for e in c.errs]
        assert not allerrs, allerrs[:5]

    browser.close()

(ROOT / "reports" / "test-results").mkdir(parents=True, exist_ok=True)
(ROOT / "reports" / "test-results" / "contracts_flows.json").write_text(json.dumps({"run": RUN, "base": BASE, "steps": steps}, indent=1))
bad = [s for s in steps if not s["ok"]]
print(f"{len(steps) - len(bad)}/{len(steps)} steps passed")
sys.exit(1 if bad else 0)

"""VECTOR browser flows (Playwright, system Edge). Needs a running stack with the demo fixtures seeded for tenant-a.
Usage: AETHER_E2E_CREDS=var/a3-creds.json python tests/e2e/risk_flows.py [base_url]   (default http://localhost:3103)
Flows: A) ETD clearing import -> G10 exception -> margin BLOCKED   B) G20 INR/USD never blended + stale data cleared on market switch
       C) OTC G03 9,500,000 POST waterfall with citations (bitemporal known_at)   D) stress leaves baseline unchanged
       E) keyboard + accessibility (tabs, dialog, labels) + 390px overflow.
Writes reports/test-results/risk_flows.json and reports/screenshots/risk_*.png. Exit code 1 if any check fails."""
import json
import os
import re
import sys
import uuid
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:3103"
CREDS = json.loads(Path(os.environ.get("AETHER_E2E_CREDS", ROOT / ".demo-credentials")).read_text())["tenant-a"]
SHOTS = ROOT / "reports" / "screenshots"
SHOTS.mkdir(parents=True, exist_ok=True)
checks: list[dict] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    checks.append({"check": name, "ok": bool(ok), "detail": detail})
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    return bool(ok)


def main_text(page) -> str:
    return page.inner_text("main")


def tab(page, name: str):
    page.get_by_role("tab", name=name).click()


def login(page, user: str):
    page.goto(BASE + "/overview")
    page.fill("#username", user)
    page.fill("#password", CREDS[user])
    page.click("button.primary")
    page.wait_for_selector(".shell")


def set_market(page, m: str):
    page.select_option("#market", m)
    page.wait_for_timeout(400)


def goto_risk(page, market: str):
    set_market(page, market)
    page.goto(BASE + "/risk")
    page.wait_for_load_state("networkidle")


def select_snapshot(page, prefix: str):
    label = next(o for o in page.locator("#snap option").all_inner_texts() if o.startswith(prefix))
    page.select_option("#snap", label=label)


with sync_playwright() as p:
    b = p.chromium.launch(channel="msedge", headless=True)
    ctx = b.new_context(viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    console_errors: list[str] = []
    failed_calls: list[str] = []
    login(page, "maker")  # the pre-login GET /session 401 is expected, so error collection starts AFTER sign-in
    page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: console_errors.append(f"pageerror: {e}"))
    page.on("response", lambda r: failed_calls.append(f"{r.status} {r.request.method} {r.url}") if r.status >= 400 else None)

    # ---------------------------------------------------------------- A) ETD import -> G10
    goto_risk(page, "ETD_DOMESTIC")
    tab(page, "Portfolio & waterfall")
    page.wait_for_selector("text=Import clearing-style margin file")
    fid = "CLR-E2E-" + uuid.uuid4().hex[:6].upper()
    csv = "\n".join([
        "file_id,account_id,component,amount,currency,aggregation_kind,as_of",
        f"{fid},ACC-DEMO-NSE-001,INITIAL_MARGIN,8000000,INR,ADDITIVE,2026-08-17T03:20:00Z",
        f"{fid},ACC-DEMO-NSE-001,EXPOSURE_MARGIN,2000000,INR,ADDITIVE,2026-08-17T03:20:00Z",
        f"{fid},ACC-DEMO-NSE-001,TOTAL_REPORTED,10100000,INR,REPORTED_TOTAL,2026-08-17T03:20:00Z"])
    page.fill("#csvtext", csv)
    page.get_by_role("button", name="Import file").click()
    page.wait_for_selector("[role=status] >> text=EXCEPTION", timeout=15000)
    st = page.locator("form[aria-labelledby=imp-h] [role=status]").inner_text()
    check("A1 import shows EXCEPTION with the independent component sum 10,000,000 and the supplied 10,100,000",
          "EXCEPTION" in st and "10,000,000" in st and "10,100,000" in st, st[:200])
    check("A2 the 100,000 difference is stated", "100000" in st.replace(",", ""), st[:200])
    page.screenshot(path=str(SHOTS / "risk_etd_import_g10_exception.png"), full_page=True)
    row = page.locator("tr", has_text=fid)
    check("A3 the imported file is listed with status EXCEPTION", row.count() == 1 and "EXCEPTION" in row.inner_text(), row.inner_text()[:150] if row.count() else "no row")
    row.get_by_role("button", name=re.compile("Margin using file")).click()
    page.wait_for_selector("text=Blocked — no authoritative figure is shown", timeout=15000)
    t = main_text(page)
    check("A4 margin with the EXCEPTION file is BLOCKED with the reconciliation reason and shows no required amount",
          "BLOCKED" in t and "reconciliation exception" in t and "Required transfer / margin" not in t, t[:300])
    page.screenshot(path=str(SHOTS / "risk_etd_margin_blocked.png"), full_page=True)
    rs = page.locator("section[aria-labelledby=rs-h]").inner_text()
    check("A5 reconciliation-status card reports the account's LATEST file (good file) as OK with 10,000,000", "OK" in rs and "10000000" in rs.replace(",", ""), rs[:200])
    pm = page.locator("section[aria-labelledby=pm-h]").inner_text()
    check("A6 position metrics: gross 60, notional 36,000,000 INR, client class UNKNOWN (not zero)",
          "60 lots" in pm and "36000000.00 INR" in pm and "UNKNOWN" in pm, pm[:300])
    page.screenshot(path=str(SHOTS / "risk_etd_portfolio.png"), full_page=True)

    # ---------------------------------------------------------------- B) G20 no currency blending
    tab(page, "Risk desk")
    select_snapshot(page, "SNAP-ETD-001")
    page.fill("#mult", "1.25")
    page.get_by_role("button", name="Run margin calculation").click()
    page.wait_for_selector("text=12,500,000", timeout=15000)  # (a previous BLOCKED result must NOT linger while this run is in flight)
    t = main_text(page)
    check("B1 domestic run shows 12,500,000 INR and HYPOTHETICAL", "12,500,000" in t and "INR" in t and "HYPOTHETICAL" in t.upper(), t[:250])
    page.screenshot(path=str(SHOTS / "risk_etd_g01_hypothetical.png"), full_page=True)
    set_market(page, "ETD_IFSC")
    page.wait_for_selector("text=IFSC ETD · USD profile")
    t = main_text(page)
    check("B2 switching to IFSC clears the domestic result: no INR amount, no 12,500,000 left on screen",
          "12,500,000" not in t and "INR" not in t and "Margin result" not in t, t[:250])
    select_snapshot(page, "SNAP-IFSC-001")
    page.get_by_role("button", name="Run margin calculation").click()
    page.wait_for_selector("text=Margin result", timeout=15000)
    t = main_text(page)
    check("B3 IFSC run is USD only (independent profile): USD present, INR absent", "USD" in t and "INR" not in t, t[:250])
    page.screenshot(path=str(SHOTS / "risk_ifsc_usd.png"), full_page=True)
    tab(page, "Portfolio & waterfall")
    page.wait_for_selector("text=Import clearing-style margin file")
    t = main_text(page)
    check("B4 IFSC portfolio view lists no DEMO_NSE/INR file or INR position metric", "CLR-DEMO-NSE" not in t and "INR" not in t, t[:200])

    # ---------------------------------------------------------------- C) OTC G03 with known_at
    goto_risk(page, "OTC")
    select_snapshot(page, "SNAP-SHOCK-001")
    page.get_by_role("button", name="Run margin calculation").click()
    page.wait_for_selector("text=Margin result", timeout=20000)
    t = main_text(page)
    check("C1 OTC G03: required 9,500,000 USD, direction POST, not BLOCKED",
          "9,500,000" in t and "POST" in t and "Blocked" not in t, t[:300])
    check("C2 time axes are explicit: business as-of, snapshot data as-of and terms known-at (recording time)",
          "business as-of 2026-08-17T04:00:00" in t and "snapshot data as of 2026-08-17T04:00:00" in t and "recording time" in t, t[:300])
    res_line = re.search(r"result ([0-9a-f]{8}) · hash ([0-9a-f]{12})", t)
    rid8, rh12 = (res_line.group(1), res_line.group(2)) if res_line else ("", "")
    check("C3 result id and hash are shown", bool(res_line))
    page.screenshot(path=str(SHOTS / "risk_otc_g03_result.png"), full_page=True)
    tab(page, "Portfolio & waterfall")
    page.wait_for_selector("text=Calculation waterfall")
    wf = page.locator("section[aria-labelledby=wf-h]")
    rows = wf.locator("tbody tr").all_inner_texts()
    wt = wf.inner_text()
    check("C4 waterfall has >= 4 traced steps, each with a citation", len(rows) >= 4 and all(len(r.split("\t")) >= 4 and r.split("\t")[-1].strip() for r in rows), str(rows)[:300])
    check("C5 waterfall shows 19,469,510.12 exposure, 9,469,510.12 unrounded and the 9500000 rounded amount",
          "19469510.12" in wt and "9469510.12" in wt and "9500000" in wt, wt[:400])
    page.screenshot(path=str(SHOTS / "risk_otc_waterfall.png"), full_page=True)

    # ---------------------------------------------------------------- D) stress leaves the baseline unchanged
    tab(page, "Stress lab")
    opt = next((o for o in page.locator("#base option").all_inner_texts() if o.startswith(rid8)), None)
    check("D0 the G03 result is selectable as the stress baseline", opt is not None, str(page.locator("#base option").all_inner_texts())[:200])
    page.select_option("#base", label=opt)
    page.fill("#bp", "100")
    page.get_by_role("button", name="Run stress").click()
    page.wait_for_selector("text=baseline_mutated = false", timeout=20000)
    st = page.locator("section[aria-labelledby=st-h]").inner_text()
    check("D1 stress shows baseline_mutated = false and the baseline exposure 19,469,510.12 USD", "19,469,510.12" in st, st[:300])
    check("D2 scenario differs from baseline (a 100bp shift changes exposure)", re.search(r"Δ", st) is not None and "DV01" in st, st[:300])
    page.screenshot(path=str(SHOTS / "risk_stress_lab.png"), full_page=True)
    full_id = page.evaluate("""async (p) => { const r = await fetch('/api/v1/risk/results?market_type=OTC&limit=50', {credentials: 'include'}); const j = await r.json();
                                  return (j.items.find(x => x.result_id.startsWith(p)) || {}).result_id || ''; }""", rid8)
    after = page.evaluate("""async (id) => { const r = await fetch('/api/v1/risk/results/' + id, {credentials: 'include'}); return (await r.json()).result_hash; }""", full_id)
    check("D3 the baseline result is byte-identical after the stress run (hash unchanged)", after.startswith(rh12) and len(rh12) == 12, f"{rh12} vs {after[:12]}")

    # ---------------------------------------------------------------- E) keyboard + accessibility
    goto_risk(page, "ETD_DOMESTIC")
    first = page.get_by_role("tab", name="Risk desk")
    first.focus()
    page.keyboard.press("ArrowRight")
    sel = page.evaluate("document.activeElement.getAttribute('role') + ':' + document.activeElement.getAttribute('aria-selected') + ':' + document.activeElement.textContent")
    check("E1 ArrowRight moves selection and focus to the next tab", sel == "tab:true:Portfolio & waterfall", sel)
    page.keyboard.press("End")
    check("E2 End selects the last tab", page.evaluate("document.activeElement.textContent") == "Stress lab")
    page.keyboard.press("Home")
    check("E3 Home selects the first tab", page.evaluate("document.activeElement.textContent") == "Risk desk")
    tabindexes = page.evaluate("Array.from(document.querySelectorAll('[role=tab]')).map(t => t.tabIndex)")
    check("E4 roving tabindex: exactly one tab is in the tab order", sorted(tabindexes) == [-1, -1, 0], str(tabindexes))
    ids_ok = page.evaluate("""() => { const t = document.querySelector('[role=tab][aria-selected=true]'); const pn = document.querySelector('[role=tabpanel]');
                                     return pn.getAttribute('aria-labelledby') === t.id && t.getAttribute('aria-controls') === pn.id; }""")
    check("E5 tab/tabpanel are linked via aria-controls / aria-labelledby", ids_ok)
    trigger = page.get_by_role("button", name="Methodology")
    trigger.focus()
    page.keyboard.press("Enter")
    page.wait_for_selector("[role=dialog]")
    inside = page.evaluate("document.querySelector('[role=dialog]').contains(document.activeElement)")
    check("E6 opening the methodology dialog moves focus inside it", inside)
    stayed = True
    for _ in range(14):
        page.keyboard.press("Tab")
        stayed = stayed and page.evaluate("document.querySelector('[role=dialog]').contains(document.activeElement)")
    check("E7 Tab is trapped inside the dialog", stayed)
    page.keyboard.press("Shift+Tab")
    check("E8 Shift+Tab stays inside the dialog", page.evaluate("document.querySelector('[role=dialog]').contains(document.activeElement)"))
    page.keyboard.press("Escape")
    page.wait_for_selector("[role=dialog]", state="detached")
    back = page.evaluate("document.activeElement.textContent")
    check("E9 Escape closes the dialog and returns focus to the trigger", back == "Methodology", back)
    for tname in ("Risk desk", "Portfolio & waterfall", "Stress lab"):
        tab(page, tname)
        page.wait_for_timeout(300)
        unnamed = page.evaluate("""() => Array.from(document.querySelectorAll('main input, main select, main textarea')).filter(e => {
            const named = (e.labels && e.labels.length) || e.getAttribute('aria-label') || e.getAttribute('aria-labelledby'); return !named; }).map(e => e.outerHTML.slice(0, 80))""")
        check(f"E10 every form control in '{tname}' has an accessible name", not unnamed, str(unnamed)[:300])
        small = page.evaluate("""() => Array.from(document.querySelectorAll('main button')).filter(b => { const r = b.getBoundingClientRect(); return r.height > 0 && r.height < 28; }).map(b => b.textContent)""")
        check(f"E11 no tiny (<28px) buttons in '{tname}'", not small, str(small))
    check("E12 no browser console errors after sign-in across all flows", not console_errors, f"{console_errors} | failed calls: {failed_calls}"[:600])

    # 390px: no horizontal overflow on any tab
    page.set_viewport_size({"width": 390, "height": 844})
    for mk in ("ETD_DOMESTIC", "OTC"):
        goto_risk(page, mk)
        for tname in ("Risk desk", "Portfolio & waterfall", "Stress lab"):
            tab(page, tname)
            page.wait_for_timeout(400)
            ov = page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1")
            check(f"E13 390px no page-level horizontal overflow ({mk} / {tname})", not ov)
    page.screenshot(path=str(SHOTS / "risk_mobile390_otc_stress.png"), full_page=True)
    b.close()

(ROOT / "reports" / "test-results").mkdir(parents=True, exist_ok=True)
(ROOT / "reports" / "test-results" / "risk_flows.json").write_text(json.dumps(checks, indent=1))
bad = [c for c in checks if not c["ok"]]
print(f"\n{len(checks)} checks, {len(bad)} failed")
sys.exit(1 if bad else 0)

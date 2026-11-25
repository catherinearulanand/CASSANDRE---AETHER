"""A2 COVENANT accessibility pass (Playwright + axe-core + explicit keyboard/focus/reflow assertions). Needs the running stack
(tests/e2e/contracts_setup.py provisioned). AXE_PATH optional (default var/plug/node_modules/axe-core/axe.min.js).
    python tests/e2e/contracts_a11y.py [base_url]
Writes reports/test-results/contracts_a11y.json and reports/screenshots/contracts_a11y_*.png. Exit 1 on any failed check."""
import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[2]
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3102"
AXE = os.environ.get("AXE_PATH") or str(ROOT / "var" / "plug" / "node_modules" / "axe-core" / "axe.min.js")
CREDS = json.loads((ROOT / "var" / "a2.creds.json").read_text())["tenant-a"]
SHOTS = ROOT / "reports" / "screenshots"
TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "best-practice"]
results: list[dict] = []


def record(name, ok, detail=None):
    results.append({"check": name, "ok": bool(ok), "detail": detail})
    print(("PASS  " if ok else "FAIL  ") + name + (f"  {detail}" if detail and not ok else ""))


def axe_scan(pg, name, include="main"):
    pg.add_script_tag(path=AXE)
    res = pg.evaluate("""async ([inc, tags]) => { const r = await axe.run({include: [[inc]]}, {runOnly: {type: 'tag', values: tags}});
        return r.violations.map(v => ({id: v.id, impact: v.impact, help: v.help, nodes: v.nodes.length,
        sample: v.nodes.slice(0,3).map(n => n.target.join(' ') + ' :: ' + (n.failureSummary||'').split('\\n').slice(1,3).join(' | '))})); }""", [include, TAGS])
    bad = [v for v in res if v["impact"] in ("serious", "critical")]
    minor = [v for v in res if v["impact"] not in ("serious", "critical")]
    record(f"axe: {name} - no serious/critical violations", not bad, bad)
    if minor:
        results.append({"check": f"axe: {name} - minor/moderate (informational)", "ok": True, "detail": minor})
        print(f"INFO  axe {name}: " + ", ".join(f"{v['id']}x{v['nodes']}" for v in minor))


def login(browser, user, width=1440):
    ctx = browser.new_context(viewport={"width": width, "height": 900})
    pg = ctx.new_page()
    pg.goto(BASE + "/contracts")
    pg.fill("#tenant", "tenant-a")
    pg.fill("#username", user)
    pg.fill("#password", CREDS[user])
    pg.click("button.primary")
    pg.wait_for_selector(".shell")
    return ctx, pg


with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True)
    ctx, pg = login(browser, "maker")
    pg.wait_for_selector("h2:has-text('Agreement registry')")
    pg.wait_for_selector("table tbody tr")
    pg.wait_for_load_state("networkidle")

    # ---- structure
    record("exactly one h1", pg.locator("h1").count() == 1, pg.locator("h1").count())
    record("no positive tabindex", pg.locator("[tabindex]:not([tabindex='0']):not([tabindex='-1'])").count() == 0)
    axe_scan(pg, "registry")

    # ---- keyboard: registry button reachable and activatable with Enter
    pg.focus("button:has-text('NS-MER-NWD-001')")
    pg.keyboard.press("Enter")
    pg.wait_for_selector("[role=tablist]")
    record("keyboard: Enter on a registry row opens the agreement", pg.locator("[role=tab]").count() == 3)

    # ---- tablist semantics + arrow keys (WAI-ARIA tabs pattern)
    tabs = pg.locator("[role=tab]")
    record("tabs: exactly one selected", pg.locator("[role=tab][aria-selected=true]").count() == 1)
    record("tabs: roving tabindex (inactive tabs tabindex=-1)", pg.locator("[role=tab][tabindex='-1']").count() == 2,
           pg.locator("[role=tab]").evaluate_all("els => els.map(e => e.getAttribute('tabindex'))"))
    record("tabs: tabpanel is labelled by its tab", pg.locator("[role=tabpanel][aria-labelledby]").count() == 1)
    pg.locator("[role=tab][aria-selected=true]").focus()
    pg.keyboard.press("ArrowRight")
    pg.wait_for_timeout(300)
    record("keyboard: ArrowRight moves selection and focus to the next tab",
           pg.locator("[role=tab][aria-selected=true]").inner_text().startswith("Freeze") and pg.evaluate("document.activeElement.getAttribute('role')") == "tab",
           pg.locator("[role=tab][aria-selected=true]").inner_text())
    pg.keyboard.press("End")
    pg.wait_for_timeout(300)
    record("keyboard: End selects the last tab", pg.locator("[role=tab][aria-selected=true]").inner_text().startswith("Eligibility"))
    pg.keyboard.press("Home")
    pg.wait_for_timeout(300)
    record("keyboard: Home selects the first tab", pg.locator("[role=tab][aria-selected=true]").inner_text().startswith("Document"))

    # ---- axe on each tab
    for label in ("Document review", "Freeze & amendments", "Eligibility & netting"):
        pg.click(f"[role=tab]:has-text('{label.split(' ')[0]}')")
        pg.wait_for_load_state("networkidle")
        pg.wait_for_timeout(400)
        axe_scan(pg, f"tab {label}")
        pg.screenshot(path=str(SHOTS / f"contracts_a11y_{label.split(' ')[0].lower()}.png"), full_page=True)

    # ---- review panel: open a field via keyboard, axe on the open panel, labelled controls
    pg.click("[role=tab]:has-text('Document')")
    pg.wait_for_selector("section[aria-labelledby='fld-h'] tbody tr")
    pg.focus("section[aria-labelledby='fld-h'] tbody tr button")
    pg.keyboard.press("Enter")
    pg.wait_for_selector("[aria-label='Field review']")
    axe_scan(pg, "open field review panel")
    unl = pg.evaluate("""() => [...document.querySelectorAll('main input, main select, main textarea')].filter(e => {
        if (e.type === 'hidden') return false; const id = e.id; return !(id && document.querySelector(`label[for="${id}"]`)) && !e.getAttribute('aria-label') && !e.getAttribute('aria-labelledby'); }).map(e => e.outerHTML.slice(0, 80))""")
    record("every form control has an accessible name", not unl, unl)
    record("selected table row is exposed (aria-selected on rows)", pg.locator("tr[aria-selected]").count() > 0)
    pg.locator("section[aria-labelledby='src-h'] pre").focus()
    record("source text viewer is keyboard-scrollable (tabindex=0 + label)", pg.evaluate("document.activeElement.tagName") == "PRE"
           and bool(pg.evaluate("document.activeElement.getAttribute('aria-label')")))
    # focus indicator visible on a focused button
    pg.focus("section[aria-labelledby='fld-h'] tbody tr button")
    outline = pg.evaluate("(() => { const s = getComputedStyle(document.activeElement); return [s.outlineStyle, s.outlineWidth, s.boxShadow]; })()")
    record("visible focus indicator on focused controls", outline[0] != "none" or outline[2] != "none", outline)

    # ---- status is not conveyed by colour alone (every status chip carries text)
    chips = pg.evaluate("() => [...document.querySelectorAll('.chip, [class*=chip]')].filter(e => !e.innerText.trim()).length")
    record("no empty status chips (status always has text)", chips == 0, chips)
    ctx.close()

    # ---- 390px reflow: no horizontal page scroll; wide tables scroll inside their own container
    ctx, mp = login(browser, "maker", width=390)
    mp.wait_for_selector("table tbody tr")
    mp.click("button:has-text('NS-MER-NWD-001')")
    mp.wait_for_selector("[role=tablist]")
    for label in ("Document", "Freeze", "Eligibility"):
        mp.click(f"[role=tab]:has-text('{label}')")
        mp.wait_for_load_state("networkidle")
        mp.wait_for_timeout(400)
        sw, cw = mp.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
        record(f"390px reflow: no page-level horizontal scroll on {label} tab", sw <= cw + 1, [sw, cw])
        mp.screenshot(path=str(SHOTS / f"contracts_a11y_mobile390_{label.lower()}.png"), full_page=True)
    axe_scan(mp, "390px Eligibility tab")
    ctx.close()
    browser.close()

(ROOT / "reports" / "test-results").mkdir(parents=True, exist_ok=True)
(ROOT / "reports" / "test-results" / "contracts_a11y.json").write_text(json.dumps(results, indent=1, default=str))
bad = [r for r in results if not r["ok"]]
print(f"{len(results) - len(bad)}/{len(results)} checks passed")
sys.exit(1 if bad else 0)

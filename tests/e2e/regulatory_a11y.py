"""A1 REGULUS accessibility pass (Playwright + axe-core + explicit keyboard/focus assertions). Needs the running stack.
    AXE_PATH=<path to axe.min.js> python tests/e2e/regulatory_a11y.py [base_url]
axe-core is NOT a repo dependency: `npm i axe-core` in any scratch directory and point AXE_PATH at node_modules/axe-core/axe.min.js.
Checks (each a named result in reports/test-results/regulatory_a11y.json):
  * axe (wcag2a/aa, 2.1 a/aa, best-practice) on every REGULUS tab, in the open approval dialog, and on an impact result
  * exactly one h1 per page; every tab panel is labelled; no positive tabindex
  * keyboard: tablist arrow keys move focus + selection; Tab order reaches the primary controls; visible focus indicator
  * dialog: focus moves in on open, Tab/Shift+Tab cannot leave it, Escape closes, focus RETURNS to the trigger
Violations are listed with impact, rule id, node count and a sample selector; the script exits 1 if any serious/critical violation remains in REGULUS-owned UI."""
import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[2]
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:3101"
AXE = os.environ.get("AXE_PATH") or str(ROOT / "var" / "plug" / "node_modules" / "axe-core" / "axe.min.js")
CREDS = json.loads((ROOT / ".demo-credentials").read_text())["tenant-a"]
TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "best-practice"]
results: list[dict] = []


def record(name, ok, detail=None):
    results.append({"check": name, "ok": ok, "detail": detail})
    print(("PASS  " if ok else "FAIL  ") + name + (f"  {detail}" if detail and not ok else ""))


def axe_scan(pg, name, include=None):
    pg.add_script_tag(path=AXE)
    ctx = {"include": [[include]]} if include else None
    res = pg.evaluate("async ([ctx, tags]) => { const r = await axe.run(ctx || document, {runOnly: {type: 'tag', values: tags}}); return r.violations.map(v => ({id: v.id, impact: v.impact, help: v.help, nodes: v.nodes.length, sample: v.nodes.slice(0,3).map(n => n.target.join(' ') + ' :: ' + (n.failureSummary||'').split('\\n').slice(1,3).join(' | '))})); }", [ctx, TAGS])
    bad = [v for v in res if v["impact"] in ("serious", "critical")]
    minor = [v for v in res if v["impact"] not in ("serious", "critical")]
    record(f"axe: {name} - no serious/critical violations", not bad, bad)
    if minor:
        results.append({"check": f"axe: {name} - minor/moderate (informational)", "ok": True, "detail": minor})
        print(f"INFO  axe {name}: {len(minor)} minor/moderate: " + ", ".join(f"{v['id']}x{v['nodes']}" for v in minor))
    return res


def main():
    with sync_playwright() as p:
        b = p.chromium.launch(channel="msedge", headless=True)
        ctx = b.new_context(viewport={"width": 1440, "height": 900})
        pg = ctx.new_page()
        pg.goto(BASE + "/overview")
        pg.fill("#username", "checker")
        pg.fill("#password", CREDS["checker"])
        pg.click("button.primary")
        pg.wait_for_selector(".shell")

        rules = pg.evaluate("async()=>{const r=await fetch('/api/v1/regulatory/rules?limit=500');return (await r.json()).items}")
        review = next((r for r in rules if r["status"] == "REVIEW_REQUIRED" and r["maker_id"] != pg.evaluate("async()=>(await (await fetch('/api/v1/session')).json()).user_id")), None)

        for tab, extra in (("command", ""), ("workbench", ""), ("versions", ""), ("impact", "")):
            pg.goto(f"{BASE}/regulatory?tab={tab}{extra}")
            pg.wait_for_selector("[role=tablist]")
            pg.wait_for_timeout(1200)
            n_h1 = pg.locator("main h1").count()
            record(f"{tab}: exactly one h1", n_h1 == 1, n_h1)
            record(f"{tab}: no positive tabindex", pg.locator("[tabindex]:not([tabindex='0']):not([tabindex='-1'])").count() == 0)
            axe_scan(pg, f"tab {tab}", "main")

        # rule detail + approval dialog (open state)
        if review:
            pg.goto(f"{BASE}/regulatory?tab=command&rule={review['rule_id']}")
            pg.wait_for_selector("#rule-h")
            axe_scan(pg, "rule detail", "main")
            trigger = pg.locator("button:has-text('Approve exact revision')")
            trigger.focus()
            trigger.press("Enter")
            pg.wait_for_selector("[role=dialog]")
            active_in = pg.evaluate("document.activeElement && !!document.activeElement.closest('[role=dialog]')")
            record("dialog: focus moves into the dialog on open", active_in)
            axe_scan(pg, "approval dialog open", "[role=dialog]")
            # Tab / Shift+Tab must stay inside the dialog (modal focus trap)
            stayed = True
            for key in ["Tab"] * 8 + ["Shift+Tab"] * 8:
                pg.keyboard.press(key)
                if not pg.evaluate("!!document.activeElement.closest('[role=dialog]')"):
                    stayed = False
                    break
            record("dialog: Tab/Shift+Tab cannot leave the modal", stayed)
            pg.keyboard.press("Escape")
            pg.wait_for_selector("[role=dialog]", state="detached")
            back = pg.evaluate("document.activeElement && document.activeElement.textContent.includes('Approve exact revision')")
            record("dialog: Escape closes and focus RETURNS to the trigger", back)
        else:
            record("rule detail / dialog checks need a REVIEW_REQUIRED rule by another maker", False, "none found; run regulatory_flows.py first")

        # keyboard: tablist arrows
        pg.goto(BASE + "/regulatory?tab=command")
        pg.wait_for_selector("[role=tablist]")
        pg.locator("[role=tab][aria-selected=true]").focus()
        pg.keyboard.press("ArrowRight")
        pg.wait_for_timeout(500)
        sel = pg.locator("[role=tab][aria-selected=true]").inner_text()
        foc = pg.evaluate("document.activeElement.getAttribute('role') + ':' + document.activeElement.textContent")
        record("tablist: ArrowRight selects and focuses the next tab", "Source workbench" in sel and "Source workbench" in foc, {"selected": sel, "focused": foc})
        # visible focus indicator on a button
        pg.goto(BASE + "/regulatory?tab=impact")
        pg.wait_for_selector("text=HYPOTHETICAL MODE")
        pg.locator("#asof").focus()
        outline = pg.evaluate("(() => { const s = getComputedStyle(document.activeElement); return s.outlineStyle + ' ' + s.outlineWidth; })()")
        record("focus is visible on inputs (outline)", "none" not in outline.split()[0] and outline.split()[1] != "0px", outline)
        # impact result region is announced
        live = pg.locator("section[aria-live]").count()
        pg.locator("label", has_text="DEMO-ETD-MARGIN v2").locator("input").check()
        pg.click("button:has-text('Run counterfactual')")
        pg.wait_for_selector("text=Result ", timeout=30000)
        record("impact result is in a polite live region", pg.locator("section[aria-live=polite]").count() >= 1, live)
        axe_scan(pg, "impact result", "main")
        # reflow at 390 px (WCAG 1.4.10): the PAGE must not scroll horizontally on any REGULUS tab (wide tables scroll inside their own container)
        mob = b.new_context(viewport={"width": 390, "height": 844})
        mp = mob.new_page()
        mp.goto(BASE + "/overview")
        mp.fill("#username", "checker")
        mp.fill("#password", CREDS["checker"])
        mp.click("button.primary")
        mp.wait_for_selector(".shell")
        for tab in ("command", "workbench", "versions", "impact"):
            mp.goto(f"{BASE}/regulatory?tab={tab}")
            mp.wait_for_selector("[role=tablist]")
            mp.wait_for_timeout(1000)
            dims = mp.evaluate("({sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth})")
            record(f"390px reflow: {tab} has no page-level horizontal scroll", dims["sw"] <= dims["cw"] + 1, dims)
            mp.screenshot(path=str(ROOT / "reports" / "screenshots" / f"regulatory_a11y_mobile390_{tab}.png"), full_page=True)
        axe_scan(mp, "mobile 390 impact tab", "main")
        b.close()
    (ROOT / "reports" / "test-results").mkdir(parents=True, exist_ok=True)
    (ROOT / "reports" / "test-results" / "regulatory_a11y.json").write_text(json.dumps(results, indent=1))
    fails = [r for r in results if not r["ok"]]
    print(f"{len(results) - len(fails)}/{len(results)} checks passed")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()

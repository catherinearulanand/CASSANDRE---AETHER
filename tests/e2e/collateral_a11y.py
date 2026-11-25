"""ATLAS keyboard + accessibility pass (Playwright/Edge + axe-core) against the stack prepared by collateral_setup.py.

    python tests/e2e/collateral_a11y.py [base_url]

Checks: (1) axe-core WCAG 2.x A/AA violations on every tab (desk, allocation w/ proposal, stress, approval); (2) tab widget is
keyboard operable (Arrow keys, roving tabindex); (3) confirmation dialog: opens from the keyboard, focus moves in, Tab/Shift+Tab
stay inside, Escape closes and focus RETURNS to the opener; (4) 200 % zoom / 320 px reflow has no horizontal page scroll;
(5) status is never colour-only (state chips carry text). Results -> reports/test-results/collateral_a11y.json
"""
import json
import re
import sys
from pathlib import Path

from axe_playwright_python.sync_playwright import Axe
from playwright.sync_api import sync_playwright

sys.argv = [sys.argv[0]] + sys.argv[1:]
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).parent))
import collateral_flows as F  # noqa: E402

RESULTS: list[dict] = []


def check(name, cond, detail=""):
    RESULTS.append({"check": name, "ok": bool(cond), "detail": str(detail)[:400]})
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))


def axe_scan(page, label):
    res = Axe().run(page, options={"runOnly": {"type": "tag", "values": ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]}})
    viol = res.response["violations"]
    summary = [f"{v['id']}({v['impact']}) x{len(v['nodes'])}: {v['nodes'][0]['html'][:110]}" for v in viol]
    check(f"axe {label}: no WCAG A/AA violations", not viol, " | ".join(summary))
    return viol


def main():
    with sync_playwright() as p:
        b = p.chromium.launch(channel="msedge", headless=True)
        # a proposal to look at (maker creates; checker views)
        mk_ctx, mk = F.login(b, "maker")
        from app.engines.collateral.fixtures import otc as otc_fixture
        import uuid
        fresh = otc_fixture()
        fresh["snapshot_id"] = "INV-OTC-A11Y-" + uuid.uuid4().hex[:6]
        F.api_post(mk, "/collateral/inventory/import", fresh)
        F.IDS["otc_snapshot"] = fresh["snapshot_id"]
        pid = F.run_optimisation(mk, F.IDS["otc_post"], F.IDS["otc_snapshot"])
        ck_ctx, ck = F.login(b, "checker")

        pages = {
            "desk": F.alloc_url(F.IDS["otc_post"], F.IDS["otc_snapshot"], "desk"),
            "allocation": F.alloc_url(F.IDS["otc_post"], F.IDS["otc_snapshot"], "allocation", pid),
            "stress": F.alloc_url(F.IDS["otc_post"], F.IDS["otc_snapshot"], "stress"),
            "approval": F.alloc_url(F.IDS["otc_post"], F.IDS["otc_snapshot"], "approval", pid),
        }
        for name, url in pages.items():
            ck.goto(url)
            ck.wait_for_load_state("networkidle")
            ck.wait_for_timeout(1200)
            axe_scan(ck, name)
            ck.screenshot(path=str(F.SHOTS / f"collateral_a11y_{name}.png"), full_page=True)

        # (2) tab widget keyboard operation
        ck.goto(pages["desk"])
        ck.wait_for_selector("[role=tab]")
        ck.focus("#col-tab-desk")
        ck.keyboard.press("ArrowRight")
        ck.wait_for_timeout(800)  # route change + re-render
        check("tabs: ArrowRight moves selection + focus to the next tab",
              ck.get_attribute("#col-tab-allocation", "aria-selected") == "true" and ck.evaluate("document.activeElement.id") == "col-tab-allocation")
        ck.keyboard.press("ArrowLeft")
        ck.wait_for_timeout(500)
        ck.keyboard.press("ArrowLeft")
        ck.wait_for_timeout(500)
        check("tabs: ArrowLeft wraps to the last tab", ck.evaluate("document.activeElement.id") == "col-tab-approval")
        check("tabs: roving tabindex (only selected tab is in the tab order)",
              ck.evaluate("[...document.querySelectorAll('[role=tab]')].filter(t=>t.tabIndex===0).length") == 1)

        # (3) dialog keyboard behaviour, using a real proposal (checker -> Approve opens the dialog)
        ck.goto(pages["approval"])
        ck.wait_for_selector("h2:has-text('Maker–checker controls')")
        btn = ck.get_by_role("button", name=re.compile(r"^Approve"))
        btn.focus()
        ck.keyboard.press("Enter")
        dlg = ck.get_by_role("dialog")
        dlg.wait_for(state="visible")
        check("dialog: opens from keyboard (Enter) and focus moves into the dialog", ck.evaluate("document.querySelector('[role=dialog]').contains(document.activeElement)"))
        inside = []
        for _ in range(10):
            ck.keyboard.press("Tab")
            inside.append(ck.evaluate("document.querySelector('[role=dialog]').contains(document.activeElement)"))
        check("dialog: Tab x10 never leaves the dialog (focus trap)", all(inside), inside)
        inside = []
        for _ in range(10):
            ck.keyboard.press("Shift+Tab")
            inside.append(ck.evaluate("document.querySelector('[role=dialog]').contains(document.activeElement)"))
        check("dialog: Shift+Tab x10 never leaves the dialog", all(inside), inside)
        check("dialog: confirm button disabled until a reason is given",
              dlg.get_by_role("button", name="Approve", exact=True).is_disabled())
        check("dialog: reason field has an accessible label", ck.locator("label[for=cd-reason]").count() == 1)
        ck.keyboard.press("Escape")
        dlg.wait_for(state="hidden")
        check("dialog: Escape closes and focus returns to the opening button",
              ck.evaluate("document.activeElement && document.activeElement.textContent.trim().startsWith('Approve')"))

        # (4) reflow: 320 CSS px wide (= 400 % zoom of 1280) has no horizontal page scroll; 200 % zoom likewise
        for w, label in ((640, "200% zoom (640px)"), (320, "400% zoom (320px)")):
            ck.set_viewport_size({"width": w, "height": 900})
            ck.goto(pages["allocation"])
            ck.wait_for_load_state("networkidle")
            ck.wait_for_timeout(800)
            over = ck.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
            check(f"reflow {label}: no horizontal page scroll (wide tables scroll inside their own container)", over <= 1, f"overflow {over}px")
        ck.set_viewport_size({"width": 1440, "height": 900})

        # (5) status is not colour-only: every state chip has text
        ck.goto(pages["allocation"])
        ck.wait_for_selector("text=/solver:/")
        chips = ck.locator(".chip")
        texts = [chips.nth(i).inner_text().strip() for i in range(chips.count())]
        check("status chips all carry text (not colour-only)", texts and all(len(t) > 1 for t in texts), texts[:12])
        # visible focus indicator
        ck.keyboard.press("Tab")
        outline = ck.evaluate("(()=>{const s=getComputedStyle(document.activeElement);return s.outlineStyle!=='none'&&parseFloat(s.outlineWidth)>0 || s.boxShadow!=='none'})()")
        check("keyboard focus indicator visible on the focused element", outline)

        for c in (mk_ctx, ck_ctx):
            c.close()
        b.close()
    bad = [r for r in RESULTS if not r["ok"]]
    out = ROOT / "reports" / "test-results"
    out.mkdir(parents=True, exist_ok=True)
    (out / "collateral_a11y.json").write_text(json.dumps({"checks": RESULTS, "passed": len(RESULTS) - len(bad), "failed": len(bad)}, indent=1))
    print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} checks passed")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()

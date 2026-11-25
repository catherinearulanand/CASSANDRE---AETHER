"""SENTINEL keyboard + accessibility pass (Playwright/Edge + axe-core). Run AFTER tests/e2e/compliance_flows.py so every screen has data.

    python tests/e2e/compliance_a11y.py [base_url]        (default http://localhost:3105)

Checks: (1) axe-core WCAG 2.x A/AA on controls, schedule, case (official + hypothetical + anomaly), surveillance, queue/waivers;
(2) tab widget keyboard operation + roving tabindex; (3) confirmation dialog: keyboard open, focus inside, Tab/Shift+Tab trapped,
Escape closes and focus returns; (4) reflow at 390 px (phone), 640 px (200 % zoom) and 320 px (400 % zoom): no horizontal page scroll;
(5) status chips carry text (not colour-only); (6) visible focus indicator; (7) every form control on the compliance screens has a label.
Results -> reports/test-results/compliance_a11y.json ; screenshots -> reports/screenshots/compliance_a11y_*.png
"""
import json
import sys
from pathlib import Path

from axe_playwright_python.sync_playwright import Axe
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent))
import compliance_flows as F  # noqa: E402

RESULTS = F.RESULTS
MSGS: list = []
check = F.check
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:3105"
OUT = F.ROOT / "reports" / "test-results" / "compliance_a11y.json"


def axe_scan(page, label):
    res = Axe().run(page, options={"runOnly": {"type": "tag", "values": ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]}})
    viol = res.response["violations"]
    summary = [f"{v['id']}({v['impact']}) x{len(v['nodes'])}: {v['nodes'][0]['html'][:120]}" for v in viol]
    check(f"axe {label}: no WCAG A/AA violations", not viol, " | ".join(summary))
    return viol


RELOADS: list = []


def settle(page, sel=None):
    """Wait for the page to be ready. If the expected element is missing after 25 s, reload (max 2x) and RECORD it: the host's Next.js proxy
    intermittently stalls the first large response (see report); a reload is not allowed to hide a real rendering failure, so it is counted."""
    page.wait_for_load_state("networkidle")
    if sel:
        for attempt in range(3):
            try:
                page.wait_for_selector(sel, timeout=25000)
                break
            except Exception:
                RELOADS.append({"url": page.url[-70:], "attempt": attempt + 1, "msgs": MSGS[-3:]})
                if attempt == 2:
                    raise
                page.reload()
                page.wait_for_load_state("networkidle")
    page.wait_for_timeout(600)


def main():
    with sync_playwright() as p:
        b = p.chromium.launch(channel="msedge", headless=True)
        co_ctx, co = F.login(b, BASE, "compliance")
        MSGS.clear()
        co.on("console", lambda m: MSGS.append((m.type, m.text[:200])))
        co.on("pageerror", lambda e: MSGS.append(("pageerror", str(e)[:300])))
        co.on("requestfailed", lambda r: MSGS.append(("requestfailed", r.url[-80:])))
        cs = F.cases(co, BASE)
        official = next(c for c in cs if c["control_key"] == "CMP-COV-ETD-INR" and not c["hypothetical"])
        hyp = next(c for c in cs if c["control_key"] == "CMP-COV-ETD-INR" and c["hypothetical"])
        anomalies = [c for c in cs if c["kind"] == "ANOMALY_INVESTIGATION" and c["state"] == "OPEN"]
        pages = {
            "controls": (f"{BASE}/compliance?tab=controls", "#sch-asof"),
            "case_official": (f"{BASE}/compliance?tab=cases&case={official['case_id']}", "#cw"),
            "case_hypothetical": (f"{BASE}/compliance?tab=cases&case={hyp['case_id']}", "#cw"),
            "surveillance": (f"{BASE}/compliance?tab=surveillance", "h2:has-text('Model card')"),
            "queue_waivers": (f"{BASE}/compliance?tab=queue", "#wv"),
        }
        if anomalies:
            pages["case_anomaly"] = (f"{BASE}/compliance?tab=cases&case={anomalies[0]['case_id']}", "#cw")
        for name, (url, sel) in pages.items():
            co.goto(url)
            settle(co, sel)
            axe_scan(co, name)
            co.screenshot(path=str(F.SHOTS / f"compliance_a11y_{name}.png"), full_page=True)

        # (7) every input/select/textarea has an accessible name (label, aria-label or aria-labelledby)
        for name, (url, sel) in pages.items():
            co.goto(url)
            settle(co, sel)
            unnamed = co.evaluate("""() => [...document.querySelectorAll('main input, main select, main textarea')].filter(e => {
                if (e.type === 'hidden') return false;
                const l = e.labels && e.labels.length; return !(l || e.getAttribute('aria-label') || e.getAttribute('aria-labelledby')); }).map(e => e.outerHTML.slice(0, 90))""")
            check(f"labels {name}: every form control has an accessible name", not unnamed, unnamed[:3])

        # (2) tab widget keyboard operation
        co.goto(pages["controls"][0])
        settle(co, "[role=tab]")
        co.focus("#cmp-tab-controls")
        co.keyboard.press("ArrowRight")
        co.wait_for_timeout(900)
        check("tabs: ArrowRight moves selection and focus to the next tab",
              co.get_attribute("#cmp-tab-cases", "aria-selected") == "true" and co.evaluate("document.activeElement.id") == "cmp-tab-cases")
        co.keyboard.press("ArrowLeft")
        co.wait_for_timeout(600)
        co.keyboard.press("ArrowLeft")
        co.wait_for_timeout(600)
        check("tabs: ArrowLeft wraps to the last tab", co.evaluate("document.activeElement.id") == "cmp-tab-queue")
        check("tabs: roving tabindex (only the selected tab is in the tab order)",
              co.evaluate("[...document.querySelectorAll('[role=tab]')].filter(t=>t.tabIndex===0).length") == 1)

        # (3) dialog keyboard behaviour on an OPEN anomaly case (compliance officer may acknowledge)
        if anomalies:
            co.goto(pages["case_anomaly"][0])
            settle(co, "#cw")
            btn = co.get_by_role("button", name="Move to ACKNOWLEDGED")
            btn.focus()
            co.keyboard.press("Enter")
            dlg = co.get_by_role("dialog")
            dlg.wait_for(state="visible")
            check("dialog: opens from the keyboard and focus moves into it", co.evaluate("document.querySelector('dialog[open]').contains(document.activeElement)"))
            inside = []
            for _ in range(8):
                co.keyboard.press("Tab")
                inside.append(co.evaluate("document.querySelector('dialog[open]').contains(document.activeElement)"))
            check("dialog: Tab x8 never leaves the dialog (focus trap)", all(inside), inside)
            inside = []
            for _ in range(8):
                co.keyboard.press("Shift+Tab")
                inside.append(co.evaluate("document.querySelector('dialog[open]').contains(document.activeElement)"))
            check("dialog: Shift+Tab x8 never leaves the dialog", all(inside), inside)
            co.keyboard.press("Escape")
            dlg.wait_for(state="hidden")
            check("dialog: Escape closes and focus returns to the opening button",
                  co.evaluate("(document.activeElement.textContent||'').trim().startsWith('Move to ACKNOWLEDGED')"))

        # (4) reflow
        for w, label in ((390, "phone 390px"), (640, "200% zoom 640px"), (320, "400% zoom 320px")):
            co.set_viewport_size({"width": w, "height": 900})
            for name in ("controls", "case_official", "queue_waivers", "surveillance"):
                url, sel = pages[name]
                co.goto(url)
                settle(co, sel)
                over = co.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
                check(f"reflow {label} {name}: no horizontal page scroll", over <= 1, f"overflow {over}px")
            if w == 390:
                co.goto(pages["controls"][0])
                settle(co, "#sch-asof")
                co.screenshot(path=str(F.SHOTS / "compliance_mobile390_controls.png"), full_page=True)
                co.goto(pages["case_official"][0])
                settle(co, "#cw")
                co.screenshot(path=str(F.SHOTS / "compliance_mobile390_case.png"), full_page=True)
        co.set_viewport_size({"width": 1440, "height": 900})

        # (5) chips carry text; (6) focus indicator
        co.goto(pages["case_official"][0])
        settle(co, "#cw")
        chips = co.locator(".chip:visible")  # chips inside a collapsed <details> are not rendered; visible ones must carry text
        texts = [chips.nth(i).inner_text().strip() for i in range(chips.count())]
        check("status chips all carry text (not colour-only)", texts and all(len(t) > 1 for t in texts), texts[:10])
        co.keyboard.press("Tab")
        outline = co.evaluate("(()=>{const s=getComputedStyle(document.activeElement);return (s.outlineStyle!=='none'&&parseFloat(s.outlineWidth)>0)||s.boxShadow!=='none'})()")
        check("keyboard focus indicator visible on the focused element", outline)
        b.close()
    bad = [r for r in RESULTS if not r["ok"]]
    OUT.write_text(json.dumps({"page_reloads_needed": RELOADS, "checks": RESULTS, "passed": len(RESULTS) - len(bad), "failed": len(bad)}, indent=1))
    print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} checks passed")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()

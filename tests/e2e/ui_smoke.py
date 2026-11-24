"""Browser smoke test (Playwright, system Edge). Not a pytest test: needs running backend+frontend and seeded demo DB.
Usage: python tests/e2e/ui_smoke.py [base_url]   -> writes reports/screenshots/*.png and reports/test-results/ui_smoke.json
Checks per route/viewport: loads, h1 present, no console errors, no failed /api calls (except expected 4xx noted), no horizontal overflow."""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:3000"
CREDS = json.loads((ROOT / ".demo-credentials").read_text())["tenant-a"]
ROUTES = ["/overview", "/regulatory", "/contracts", "/risk", "/collateral", "/compliance", "/evidence", "/scenarios", "/methodology", "/about"]
VIEWPORTS = {"desktop1440": (1440, 900), "laptop1280": (1280, 800), "tablet768": (768, 1024), "mobile390": (390, 844)}
OUT = ROOT / "reports" / "screenshots"
OUT.mkdir(parents=True, exist_ok=True)
results = []

with sync_playwright() as p:
    b = p.chromium.launch(channel="msedge", headless=True)
    for vp, (w, h) in VIEWPORTS.items():
        ctx = b.new_context(viewport={"width": w, "height": h})
        page = ctx.new_page()
        errs, bad = [], []
        page.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errs.append(f"pageerror: {e}"))
        page.on("response", lambda r: bad.append(f"{r.status} {r.url}") if "/api/v1" in r.url and r.status >= 400 else None)
        page.goto(BASE + "/overview")
        page.fill("#username", "maker")
        page.fill("#password", CREDS["maker"])
        page.click("button.primary")
        page.wait_for_selector(".shell")
        for r in ROUTES:
            errs.clear(); bad.clear()
            page.goto(BASE + r)
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(600)
            h1 = page.locator("h1").first.inner_text() if page.locator("h1").count() else None
            overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1")
            body = page.inner_text("main")
            name = f"{vp}{r.replace('/', '_')}.png"
            page.screenshot(path=str(OUT / name), full_page=True)
            results.append({"viewport": vp, "route": r, "h1": h1, "horizontal_overflow": overflow,
                            "not_built_banner": "NOT IMPLEMENTED" in body, "console_errors": list(errs), "failed_api": list(bad)})
        ctx.close()
    b.close()

(ROOT / "reports" / "test-results").mkdir(parents=True, exist_ok=True)
(ROOT / "reports" / "test-results" / "ui_smoke.json").write_text(json.dumps(results, indent=1))
problems = [r for r in results if r["horizontal_overflow"] or r["console_errors"] or r["failed_api"] or not r["h1"] or r["not_built_banner"]]
print(f"{len(results)} route-views checked; {len(problems)} with problems")
for r in problems:
    print(r["viewport"], r["route"], {k: v for k, v in r.items() if k in ("h1", "horizontal_overflow", "not_built_banner", "console_errors", "failed_api") and v})
sys.exit(1 if problems else 0)

"""Browser flow: Risk desk -> pin SNAP-ETD-001 -> hypothetical multiplier 1.25 -> Run margin calculation.
Asserts the UI shows INR 12,500,000 (independent G01 hand value) and a HYPOTHETICAL marker. Needs running stack + seeded demo DB."""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:3000"
pw = json.loads((ROOT / ".demo-credentials").read_text())["tenant-a"]["maker"]
with sync_playwright() as p:
    b = p.chromium.launch(channel="msedge", headless=True)
    pg = b.new_page(viewport={"width": 1440, "height": 900})
    api = []
    pg.on("response", lambda r: api.append((r.request.method, r.status, r.url)) if "/api/v1/risk" in r.url and r.request.method == "POST" else None)
    pg.goto(BASE + "/overview")
    pg.fill("#username", "maker")
    pg.fill("#password", pw)
    pg.click("button.primary")
    pg.wait_for_selector(".shell")
    pg.select_option("#market", "ETD_DOMESTIC")
    pg.goto(BASE + "/risk")
    pg.wait_for_load_state("networkidle")
    pg.locator("select").nth(1).select_option(label=pg.locator("select").nth(1).locator("option").nth(1).inner_text())
    snap_txt = pg.locator("select").nth(1).locator("option:checked").inner_text()
    pg.fill("input[placeholder='e.g. 1.25']", "1.25")
    pg.click("text=Run margin calculation")
    pg.wait_for_timeout(2500)
    text = pg.inner_text("main")
    pg.screenshot(path=str(ROOT / "reports" / "screenshots" / "flow_g01_risk_result.png"), full_page=True)
    print("snapshot chosen:", snap_txt)
    print("POST calls:", api)
    ok = "12,500,000" in text and "HYPOTHETICAL" in text.upper()
    print("shows 12,500,000:", "12,500,000" in text, "| HYPOTHETICAL marker:", "HYPOTHETICAL" in text.upper())
    b.close()
    sys.exit(0 if ok else 1)

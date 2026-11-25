"""Evidence browser flows (A6). Needs: backend + frontend running against a DB prepared with tests/e2e/prepare_evidence_demo.py
(ids in var/a6-demo-ids.json, credentials in var/a6-creds.json). Playwright + system Edge.

    python tests/e2e/evidence_flows.py [base_url]

Writes reports/test-results/evidence_flows.json and reports/screenshots/evidence_*.png; exit code 1 if any check fails.
Every check is a real browser interaction against the real API; nothing is mocked."""
import json
import os
import subprocess
import sys
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
BASE = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("A6_BASE", "http://localhost:3106")
CREDS = json.loads((ROOT / "var" / "a6-creds.json").read_text())
IDS = json.loads((ROOT / "var" / "a6-demo-ids.json").read_text())
SHOTS = ROOT / "reports" / "screenshots"
SHOTS.mkdir(parents=True, exist_ok=True)
AXE = (ROOT / "var" / "axe-tool" / "node_modules" / "axe-core" / "axe.min.js")
RESULTS: list[dict] = []
FINDINGS: dict = {"axe": {}, "contrast_and_labels": {}}


def check(name, cond, detail=""):
    RESULTS.append({"check": name, "ok": bool(cond), "detail": str(detail)[:400]})
    print(("PASS " if cond else "FAIL ") + name + (f"  [{str(detail)[:160]}]" if (detail and not cond) else ""))
    return bool(cond)


def login(browser, tenant, user, width=1440, height=900):
    ctx = browser.new_context(viewport={"width": width, "height": height}, accept_downloads=True)
    page = ctx.new_page()
    errs = []
    page.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errs.append(f"pageerror: {e}"))
    page._http = []  # type: ignore[attr-defined]
    page.on("response", lambda r: page._http.append((r.status, r.url.replace(BASE, ""))) if r.status >= 400 else None)  # type: ignore[attr-defined]
    page.goto(BASE + "/overview")
    page.fill("#tenant", tenant)
    page.fill("#username", user)
    page.fill("#password", CREDS[tenant][user])
    page.click("button.primary")
    page.wait_for_selector(".shell")
    page._errs = errs  # type: ignore[attr-defined]
    return ctx, page


def api_get(page, path):
    r = page.request.get(f"{BASE}/api/v1{path}")
    return r.status, (r.json() if r.status < 500 and "json" in (r.headers.get("content-type") or "") else None)


def head_seq(page):
    st, j = api_get(page, "/evidence/events?limit=1")
    return j["items"][0]["sequence"] if st == 200 and j["items"] else None


def goto(page, path):
    page.goto(BASE + path)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(400)


def shot(page, name):
    page.screenshot(path=str(SHOTS / f"evidence_{name}.png"), full_page=True)


def rows(page):
    return page.locator("table tbody tr")


def cli_verify(bundle: Path, pub: Path):
    return subprocess.run([sys.executable, "-m", "tools.verify_bundle", str(bundle), "--trusted-key", str(pub)], cwd=ROOT,
                          capture_output=True, text=True, env={"PYTHONPATH": str(ROOT), "SYSTEMROOT": os.environ.get("SYSTEMROOT", "C:\\Windows")})


def tab(page, name):
    page.get_by_role("tab", name=name).click()
    page.wait_for_timeout(300)


# ------------------------------------------------------------------------------------------------------ flows
def flow_auditor(b):
    ctx, page = login(b, "tenant-a", "auditor")
    goto(page, "/evidence")
    check("auditor: page h1 and four tabs", page.locator("h1").first.inner_text().startswith("Evidence & Audit") and page.get_by_role("tab").count() == 4)
    expect(page.get_by_text("Full detail")).to_be_visible()
    check("auditor: sees FULL detail level", True)
    page.get_by_role("button", name="Verify chain against signed checkpoints").click()
    page.wait_for_selector("text=Chain valid", timeout=20000)
    check("auditor: chain verified against signed checkpoint from the UI", "ANCHORED" in page.inner_text("main"))
    # filters
    page.fill("#f-engine", "risk")
    page.get_by_role("button", name="Search").click()
    page.wait_for_timeout(600)
    engines = {r.locator("td").nth(2).inner_text() for r in rows(page).all()}
    check("audit filter engine=risk returns only risk events", engines == {"risk"} and rows(page).count() > 0, engines)
    page.fill("#f-engine", "")
    page.fill("#f-action", "CONTROL_EVALUATED")
    page.get_by_role("button", name="Search").click()
    page.wait_for_timeout(600)
    acts = {r.locator("td").nth(3).inner_text() for r in rows(page).all()}
    check("audit filter action=CONTROL_EVALUATED returns only that action", acts == {"CONTROL_EVALUATED"}, acts)
    page.get_by_role("button", name="Clear").click()
    page.fill("#f-entity_id", IDS["g01_margin_result"])
    page.get_by_role("button", name="Search").click()
    page.wait_for_timeout(600)
    check("audit filter by entity id finds the margin result event", rows(page).count() >= 1 and "risk" in rows(page).first.inner_text())
    seq = rows(page).first.locator("td").first.inner_text().strip()
    rows(page).first.get_by_role("button").click()
    page.wait_for_selector("aside[aria-label*='technical details']")
    check("event detail drawer shows hashes and actor (full detail)", "prev_hash" in page.inner_text("aside") and "event_hash" in page.inner_text("aside"))
    shot(page, "audit_explorer")
    goto(page, f"/evidence?seq={seq}")
    page.wait_for_selector("aside[aria-label*='technical details']", timeout=8000)
    check("deep link /evidence?seq=N opens that event's technical details", f"Event {seq}" in page.inner_text("aside"))

    # lineage by run
    goto(page, f"/evidence?run={IDS['g01_run']}")
    page.wait_for_selector("text=Stepwise trace", timeout=15000)
    txt = page.inner_text("main")
    check("lineage G01: INCOMPLETE is stated explicitly", "Lineage INCOMPLETE" in txt)
    for label in ("Approved rule / terms", "Pinned snapshot", "Margin result", "Allocation proposal", "Control / case"):
        check(f"lineage G01 shows stage '{label}'", label in txt)
    check("lineage G01 lists missing stage Approval explicitly", "Missing stage: Approval" in txt)
    check("lineage G01 labels seed-time ancestors as recorded earlier", "Recorded earlier" in txt)
    check("lineage G01 orders steps (>=8 list items)", page.locator("ol > li").count() >= 8, page.locator("ol > li").count())
    shot(page, "lineage_g01")
    goto(page, f"/evidence?run={IDS['bare_run']}")
    page.wait_for_selector("text=Stepwise trace", timeout=15000)
    t2 = page.inner_text("main")
    check("lineage of a bare run shows many explicit missing stages", t2.count("Missing stage:") >= 6, t2.count("Missing stage:"))
    goto(page, f"/evidence?entity={IDS['g01_margin_result']}")
    page.wait_for_selector("[data-testid=entity-banner]", timeout=15000)
    page.wait_for_selector("text=Stepwise trace", timeout=15000)
    check("deep link ?entity=<margin result id> resolves to the decision lineage", IDS["g01_margin_result"] in page.inner_text("[data-testid=entity-banner]"))
    goto(page, "/evidence?entity=NO-SUCH-ENTITY-123")
    page.wait_for_selector("text=No evidence references this id", timeout=10000)
    check("unknown entity id shows an explicit not-found state", True)
    goto(page, f"/evidence?run={IDS['tenantb_run']}")
    page.wait_for_selector("[role=alert]", timeout=10000)
    alert = page.inner_text("[role=alert]")
    check("tenant-A auditor cannot open tenant-B run: NOT_FOUND, no data", "NOT_FOUND" in alert and IDS["tenantb_run"][:8] not in page.inner_text("ol") if page.locator("ol").count() else "NOT_FOUND" in alert, alert)

    # replay (read-only)
    goto(page, f"/evidence?tab=lab&run={IDS['g01_run']}")
    page.wait_for_timeout(500)
    before = head_seq(page)
    page.get_by_role("button", name="Replay (read-only)").click()
    page.wait_for_selector("text=replay recorded as its own event", timeout=30000)
    page.wait_for_timeout(500)
    lab = page.inner_text("main")
    check("replay of G01 run reports MATCH for deterministic engine events", "MATCH" in lab and "NOT_REPLAYABLE" not in lab.split("Replay (read-only)")[-1][:600], lab[-300:])
    check("replay table lists replayed events with engine", page.locator("table").last.locator("tbody tr").count() >= 3)
    after = head_seq(page)
    check("replay is read-only: chain head advanced by exactly 1 (its own REPLAY_COMPLETED event)", before is not None and after == before + 1, (before, after))
    shot(page, "replay_lab")

    # tamper demonstrations on COPIED bundle; operational evidence untouched
    head_before = head_seq(page)
    page.get_by_role("button", name="Create export for selected run").click()
    page.wait_for_selector("text=export ", timeout=30000)
    for mode, label, expect_ok in (("none", "No change (control)", True), ("modify_event", "Modify an event", False),
                                   ("drop_event", "Drop an event", False), ("truncate_chain_skeleton", "Truncate chain tail", False),
                                   ("wrong_trusted_key", "Verify with the wrong key", False)):
        page.select_option("#tmode", mode)
        page.get_by_role("button", name="Run tamper test").click() if page.get_by_role("button", name="Run tamper test").count() else page.locator("button.primary").last.click()
        page.wait_for_timeout(1200)
        txt = page.inner_text("main")
        seg = txt.split("Tamper demonstration")[-1]
        ok_seen = "Verified" in seg and "FAILED" not in seg[:200]
        check(f"tamper demo [{mode}] -> {'verified' if expect_ok else 'verification FAILED'}", (ok_seen if expect_ok else "Verification FAILED" in seg), seg[:200])
    head_after = head_seq(page)
    check("tamper demo never modified operational evidence (head only moved by the export bookkeeping)", head_after in (head_before, head_before + 1, head_before + 2), (head_before, head_after))
    shot(page, "tamper_demo")

    # exports + offline verification
    goto(page, f"/evidence?tab=exports&run={IDS['g01_run']}")
    page.get_by_role("button", name="Export signed bundle").click()
    page.get_by_role("button", name="Confirm export").click()
    page.wait_for_selector("text=Preview · export", timeout=30000)
    check("export preview says FULL content for an auditor", "Full content" in page.inner_text("main"))
    check("export preview states it is an internal evidence pack, not a regulatory submission", "internal evidence pack" in page.inner_text("main"))
    with page.expect_download(timeout=30000) as dl:
        page.get_by_role("link", name="Download").first.click()
    out = ROOT / "var" / "a6_dl_bundle.zip"
    dl.value.save_as(str(out))
    _, keys = api_get(page, "/evidence/public-keys")
    pub = ROOT / "var" / "a6_trusted.pub"
    pub.write_text(next(k for k in keys["items"] if k["current"])["public_key_pem"])
    r = cli_verify(out, pub)
    check("downloaded bundle verifies with the offline verifier (exit 0)", r.returncode == 0, r.stdout + r.stderr)
    raw = bytearray(out.read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    bad = ROOT / "var" / "a6_dl_bundle_tampered.zip"
    bad.write_bytes(bytes(raw))
    r2 = cli_verify(bad, pub)
    check("TAMPERED copy of the downloaded bundle fails the offline verifier (non-zero exit)", r2.returncode != 0, r2.stdout + r2.stderr)
    shot(page, "exports")
    unexpected = [h for h in page._http if not (h[0] == 401 and h[1].endswith("/session")) and not (h[0] == 404 and (IDS["tenantb_run"] in h[1] or "NO-SUCH-ENTITY" in h[1]))]  # type: ignore[attr-defined]
    check("auditor: no unexpected HTTP errors (only pre-login 401 and the two deliberate 404 probes)", not unexpected, unexpected[:4])
    check("auditor: no uncaught JS exceptions", not [e for e in page._errs if e.startswith("pageerror")], page._errs[:3])  # type: ignore[attr-defined]
    ctx.close()


def flow_admin_and_viewer(b):
    ctx, page = login(b, "tenant-a", "admin")
    goto(page, f"/evidence?tab=exports&run={IDS['g01_run']}")
    page.get_by_role("button", name="Export signed bundle").click()
    page.get_by_role("button", name="Confirm export").click()
    page.wait_for_selector("text=Preview · export", timeout=30000)
    txt = page.inner_text("main")
    check("tenant_admin export is forced REDACTED (cannot obtain full event content)", "REDACTED" in txt and "Full content" not in txt, txt[:200])
    shot(page, "export_admin_redacted")
    ctx.close()

    ctx, page = login(b, "tenant-a", "viewer")
    goto(page, "/evidence")
    expect(page.get_by_text("Metadata only (role)")).to_be_visible()
    check("viewer: audit explorer is METADATA ONLY", True)
    page.get_by_role("button", name="Verify chain against signed checkpoints").click()
    page.wait_for_selector("text=FORBIDDEN", timeout=10000)
    check("viewer: chain verification is FORBIDDEN (explicit error with correlation id)", "FORBIDDEN" in page.inner_text("main") and "correlation_id" in page.inner_text("main"))
    first = rows(page).first
    first.get_by_role("button").click()
    page.wait_for_selector("aside[aria-label*='technical details']")
    side = page.inner_text("aside")
    check("viewer: event details hide actor/rationale", "hidden for your role" in side and "payload" not in side.lower().replace("payload_", ""), side[:200])
    goto(page, "/evidence?tab=exports")
    page.wait_for_timeout(800)
    check("viewer: exports tab shows an error state, no export list", page.locator("[role=alert]").count() >= 1 and "FORBIDDEN" in page.inner_text("main"))
    shot(page, "viewer_forbidden")
    ctx.close()

    ctx, page = login(b, "tenant-b", "auditor")
    goto(page, "/evidence")
    page.fill("#f-entity_id", IDS["g01_margin_result"])
    page.get_by_role("button", name="Search").click()
    page.wait_for_timeout(800)
    check("tenant-B auditor searching tenant-A's entity id gets no events (no leakage)", "No events match" in page.inner_text("main"))
    ctx.close()


def flow_truncation(b):
    """Privileged truncation of tenant-B's chain tail (DB level, drill only) -> UI verification against the trusted checkpoint FAILS."""
    code = ("import sys;sys.path.insert(0,'backend');from sqlalchemy import text;from app.db.base import SessionLocal\n"
            "with SessionLocal() as s:\n"
            " m=s.execute(text(\"select max(sequence) from ev_events where tenant_id='tenant-b'\")).scalar_one()\n"
            " s.execute(text(\"delete from ev_events where tenant_id='tenant-b' and sequence=:m\"),{'m':m})\n"
            " s.execute(text(\"update ev_chain_heads set sequence=:p, head_hash=(select event_hash from ev_events where tenant_id='tenant-b' and sequence=:p) where tenant_id='tenant-b'\"),{'p':m-1})\n"
            " s.commit();print('truncated',m)")
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, env=os.environ)
    check("drill: tenant-B chain tail truncated at DB level", r.returncode == 0, r.stderr[-300:])
    ctx, page = login(b, "tenant-b", "auditor")
    goto(page, "/evidence")
    page.get_by_role("button", name="Verify chain against signed checkpoints").click()
    page.wait_for_selector("text=Chain INVALID", timeout=20000)
    txt = page.inner_text("main")
    check("UI: truncation detected against the trusted signed checkpoint", "CHECKPOINT_NOT_REPRODUCED" in txt or "CHAIN_SHORTER_THAN_ANCHOR" in txt, txt[:300])
    shot(page, "truncation_detected")
    goto(page, "/evidence?tab=lab")
    page.wait_for_selector("text=Integrity dashboard")
    page.wait_for_timeout(500)
    check("integrity dashboard reports FAILED for the truncated tenant", "FAILED" in page.inner_text("main") and "CHAIN_INVALID" in page.inner_text("main"))
    ctx.close()


# ------------------------------------------------------------------------------------------------ accessibility
CONTRAST_JS = """() => {
  const lum = c => { const [r,g,b] = c.map(v => { v/=255; return v<=0.03928 ? v/12.92 : Math.pow((v+0.055)/1.055, 2.4); }); return 0.2126*r+0.7152*g+0.0722*b; };
  const parse = s => { const m = s.match(/rgba?\\(([^)]+)\\)/); if (!m) return null; const p = m[1].split(',').map(x => parseFloat(x)); return {c: p.slice(0,3), a: p.length>3 ? p[3] : 1}; };
  const bgOf = el => { while (el) { const p = parse(getComputedStyle(el).backgroundColor); if (p && p.a > 0.9) return p.c; el = el.parentElement; } return [16,24,23]; };
  const bad = []; let n = 0;
  document.querySelectorAll('main *, nav *, header *').forEach(el => {
    if (!el.childNodes.length) return;
    const own = Array.from(el.childNodes).filter(x => x.nodeType === 3 && x.textContent.trim().length > 1);
    if (!own.length) return;
    const r = el.getBoundingClientRect(); if (r.width === 0 || r.height === 0) return;
    const cs = getComputedStyle(el); const fg = parse(cs.color); if (!fg) return;
    const bg = bgOf(el); const L1 = lum(fg.c), L2 = lum(bg); const ratio = (Math.max(L1,L2)+0.05)/(Math.min(L1,L2)+0.05);
    const size = parseFloat(cs.fontSize); const bold = parseInt(cs.fontWeight) >= 700; const large = size >= 24 || (size >= 18.66 && bold);
    n++; if (ratio < (large ? 3 : 4.5)) bad.push({text: own[0].textContent.trim().slice(0,40), ratio: Math.round(ratio*100)/100, fg: cs.color, size});
  });
  return {checked: n, below_aa: bad.slice(0, 12), below_count: bad.length};
}"""
LABEL_JS = """() => {
  const out = {unlabelled_controls: [], unnamed_buttons_links: [], h1: document.querySelectorAll('h1').length, lang: document.documentElement.lang,
               live_regions: document.querySelectorAll('[aria-live]').length, tables_without_caption_or_heading: 0};
  document.querySelectorAll('main input, main select, main textarea').forEach(e => {
    if (e.type === 'hidden') return;
    const id = e.id; const labelled = (id && document.querySelector('label[for="'+id+'"]')) || e.getAttribute('aria-label') || e.getAttribute('aria-labelledby') || e.closest('label');
    if (!labelled) out.unlabelled_controls.push(e.outerHTML.slice(0, 80));
  });
  document.querySelectorAll('main button, main a').forEach(e => { const name = (e.innerText || e.getAttribute('aria-label') || e.title || '').trim(); if (!name) out.unnamed_buttons_links.push(e.outerHTML.slice(0, 80)); });
  return out;
}"""


def flow_a11y(b):
    ctx, page = login(b, "tenant-a", "auditor")
    tabs = [("audit", "/evidence"), ("lineage", f"/evidence?tab=lineage&run={IDS['g01_run']}"), ("lab", f"/evidence?tab=lab&run={IDS['g01_run']}"),
            ("exports", f"/evidence?tab=exports&run={IDS['g01_run']}")]
    # keyboard: roving tabindex + arrow keys on the tablist
    goto(page, "/evidence")
    page.get_by_role("tab").first.focus()
    page.keyboard.press("ArrowRight")
    page.wait_for_timeout(300)
    sel = page.locator("[role=tab][aria-selected=true]").inner_text()
    check("keyboard: ArrowRight moves to the next tab and selects it", sel.startswith("Decision lineage"), sel)
    page.keyboard.press("ArrowLeft")
    page.keyboard.press("ArrowLeft")
    page.wait_for_timeout(300)
    check("keyboard: ArrowLeft wraps to the last tab", page.locator("[role=tab][aria-selected=true]").inner_text().startswith("Evidence exports"))
    page.get_by_role("tab").first.click()
    page.keyboard.press("Tab")
    focused = page.evaluate("document.activeElement && (document.activeElement.innerText || document.activeElement.id || document.activeElement.tagName)")
    outline = page.evaluate("(() => { const cs = getComputedStyle(document.activeElement); return [cs.outlineStyle, cs.outlineWidth]; })()")
    check("keyboard: Tab reaches the first control in the panel and shows a visible focus outline", outline[0] != "none" and outline[1] != "0px", (focused, outline))
    # axe + contrast + label scans per tab and viewport
    axe_src = AXE.read_text(encoding="utf-8")
    for name, path in tabs:
        goto(page, path)
        if name == "lab":
            page.wait_for_timeout(800)
        page.add_script_tag(content=axe_src)
        res = page.evaluate("axe.run(document, {runOnly: {type: 'tag', values: ['wcag2a','wcag2aa','wcag21a','wcag21aa','best-practice']}}).then(r => ({violations: r.violations.map(v => ({id: v.id, impact: v.impact, nodes: v.nodes.length, help: v.help, sample: v.nodes[0].html.slice(0, 120)})), passes: r.passes.length}))")
        FINDINGS["axe"][name] = res
        serious = [v for v in res["violations"] if v["impact"] in ("serious", "critical")]
        check(f"axe-core [{name}]: no serious/critical WCAG A/AA violations", not serious, json.dumps(serious)[:300])
        con = page.evaluate(CONTRAST_JS)
        lab = page.evaluate(LABEL_JS)
        FINDINGS["contrast_and_labels"][name] = {"contrast": con, "labels": lab}
        check(f"contrast [{name}]: all sampled text meets WCAG AA ({con['checked']} nodes)", con["below_count"] == 0, json.dumps(con["below_aa"])[:300])
        check(f"labels [{name}]: every control labelled, no unnamed buttons/links, single h1, lang set",
              not lab["unlabelled_controls"] and not lab["unnamed_buttons_links"] and lab["h1"] == 1 and bool(lab["lang"]), json.dumps(lab)[:300])
        shot(page, f"a11y_{name}_1440")
    ctx.close()
    # responsive: no document-level horizontal overflow at 4 widths, screenshots at 1440/390
    for w, h in ((1440, 900), (1280, 800), (768, 1024), (390, 844)):
        ctx, page = login(b, "tenant-a", "auditor", w, h)
        for name, path in tabs:
            goto(page, path)
            ov = page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1")
            check(f"responsive {w}px [{name}]: no horizontal page overflow", not ov)
            if w == 390:
                shot(page, f"{name}_390")
        ctx.close()


def main():
    with sync_playwright() as p:
        b = p.chromium.launch(channel="msedge", headless=True)
        for fn in (flow_auditor, flow_admin_and_viewer, flow_a11y, flow_truncation):
            try:
                fn(b)
            except (PWTimeout, AssertionError, KeyError) as e:
                check(f"{fn.__name__} completed without an exception", False, repr(e)[:300])
        b.close()
    out = ROOT / "reports" / "test-results" / "evidence_flows.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"results": RESULTS, "findings": FINDINGS}, indent=1))
    fails = [r for r in RESULTS if not r["ok"]]
    print(f"\n{len(RESULTS)} checks, {len(fails)} failed")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()

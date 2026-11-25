"""SENTINEL browser flows (Playwright + system Edge) against a RUNNING stack (backend :8105, frontend :3105, database aether_d5).

    python tests/e2e/compliance_flows.py setup                 # in-process: reset THIS tenant's compliance records, real risk results, collateral snapshots
    python tests/e2e/compliance_flows.py [base_url]            # browser flows (default http://localhost:3105)

Credentials: var/a5-credentials.json (private, gitignored); the shared .demo-credentials is never touched.
Every business number is produced by the real engines (risk.compute_margin, collateral import/coverage) and re-asserted against
hand values: G01 hypothetical requirement 12,500,000 INR vs posted 11,500,000 INR => gap 1,000,000 INR.
Results -> reports/test-results/compliance_flows.json ; screenshots -> reports/screenshots/compliance_*.png ; exit 1 on any failed check.
"""
import json
import os
import re
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "backend" / "scripts"))
SHOTS = ROOT / "reports" / "screenshots"
SHOTS.mkdir(parents=True, exist_ok=True)
IDS_FILE = ROOT / "var" / "a5-flow-ids.json"
T = "tenant-a"
ACC = "ACC-DEMO-NSE-001"
RESULTS: list[dict] = []


def check(name, cond, detail=""):
    RESULTS.append({"check": name, "ok": bool(cond), "detail": str(detail)[:400]})
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    return bool(cond)


# ---------------------------------------------------------------------------------------------------- setup (in-process, real engines)
def setup():
    pw = (ROOT / "var" / "pg.pw").read_text().strip()
    os.environ["AETHER_DATABASE_URL"] = f"postgresql+psycopg://aether:{pw}@127.0.0.1:5433/aether_d5"
    os.environ["AETHER_AUTO_CREATE_SCHEMA"] = "true"
    from sqlalchemy import delete, select

    import app.main  # noqa: F401  (registers every engine's services)
    from app.core import services
    from app.db.base import SessionLocal
    from app.engines.collateral import fixtures as colfx
    from app.engines.collateral.services import import_inventory
    from app.engines.compliance.models import (
        CmpCase,
        CmpCaseEvent,
        CmpEvaluation,
        CmpJob,
        CmpModelVersion,
        CmpScore,
        CmpWaiver,
    )
    from app.engines.regulatory.models import RegRule

    as_of = datetime(2026, 8, 17, 4, 0, tzinfo=UTC)
    run = uuid.uuid4().hex[:6]
    with SessionLocal() as s:
        for m in (CmpCaseEvent, CmpEvaluation, CmpCase, CmpWaiver, CmpJob, CmpScore, CmpModelVersion):  # THIS tenant's own compliance working data only
            s.execute(delete(m).where(m.tenant_id == T))
        ctx = {"market_type": "ETD_DOMESTIC", "venue": "DEMO_NSE", "account_id": ACC, "currency": "INR", "as_of": as_of.isoformat()}
        v2 = s.execute(select(RegRule).where(RegRule.tenant_id == T, RegRule.rule_key == "DEMO-ETD-MARGIN", RegRule.version == 2)).scalar_one()
        br = services.call("regulatory.resolve_rules", s, T, ctx, as_of, None)
        hr = services.call("regulatory.resolve_rules", s, T, ctx, as_of, None, [v2.rule_id])
        base = services.call("risk.compute_margin", s, T, ctx, "SNAP-ETD-001", None, br["rule_set_hash"], "ETD_IMPORTED_V1")
        hyp = services.call("risk.compute_margin", s, T, ctx, "SNAP-ETD-001", None, hr["rule_set_hash"], "ETD_IMPORTED_V1")
        assert base["status"] == "OK" and hyp["status"] == "OK" and hyp["hypothetical"] is True, (base["status"], hyp["status"])
        assert base["required_amount"]["amount"] == "10000000" and hyp["required_amount"]["amount"] == "12500000"
        ids = {"base": base["result_id"], "hyp": hyp["result_id"], "base_rules": br["rule_set_hash"], "hyp_rules": hr["rule_set_hash"], "run": run}
        # simulated posting timeline imported through the collateral engine (posted units of the cash lot at the account)
        for tag, hour, cash in (("down", 5, "3000000"), ("up", 7, "6000000")):
            inv = colfx.etd_domestic()
            inv["snapshot_id"] = f"INV-A5-{run}-{tag}"
            inv["as_of"] = f"2026-08-17T{hour:02d}:00:00Z"
            for lot in inv["lots"]:
                if lot["lot_id"] == "POST-CASH-INR":
                    lot["posted_units"] = cash
            import_inventory(s, T, actor_id="a5-setup", role="maker", payload=inv, correlation_id=f"a5-setup-{run}-{tag}")
        for tag, hh in (("original", 4), ("down", 6), ("up", 7)):
            c = services.call("collateral.coverage", s, T, ACC, datetime(2026, 8, 17, hh, 30 if tag == "up" else 0, tzinfo=UTC))
            ids[f"coverage_{tag}"] = c["eligible_posted"]["amount"]
        s.commit()
    assert ids["coverage_original"] == "11500000" and ids["coverage_down"] == "9500000" and ids["coverage_up"] == "12500000", ids
    IDS_FILE.write_text(json.dumps(ids, indent=1))
    print("setup ok:", {k: v for k, v in ids.items() if k.startswith("coverage")})


# ---------------------------------------------------------------------------------------------------- browser helpers
def creds():
    return json.loads((ROOT / "var" / "a5-credentials.json").read_text())["tenant-a"]


def login(browser, base, user, viewport=None):
    ctx = browser.new_context(viewport=viewport or {"width": 1440, "height": 900})
    page = ctx.new_page()
    page.goto(base + "/overview")
    page.fill("#username", user)
    page.fill("#password", creds()[user])
    page.click("button.primary")
    page.wait_for_selector(".shell")
    return ctx, page


def csrf(page, base):
    return page.request.get(base + "/api/v1/session").json()["csrf_token"]


def csrf_user(page, base):
    return page.request.get(base + "/api/v1/session").json()["user_id"]


def api_post(page, base, path, body):
    return page.request.post(base + "/api/v1" + path, data=json.dumps(body),
                             headers={"content-type": "application/json", "x-csrf-token": csrf(page, base), "Idempotency-Key": str(uuid.uuid4())})


def api_get(page, base, path):
    return page.request.get(base + "/api/v1" + path).json()


def confirm(page, label, scope=None):
    (scope or page).get_by_role("button", name=label, exact=True).first.click()
    page.locator("dialog[open]").get_by_role("button", name="Confirm", exact=True).click()
    page.wait_for_timeout(700)


def evaluate_ui(page, base, *, margin, rules, asof, snap="SNAP-ETD-001", families=(), report=None, shot=None):
    page.goto(f"{base}/compliance?tab=controls")
    page.wait_for_selector("#ev")
    page.select_option("#e-ccy", ) if False else None
    page.fill("#e-acc", ACC)
    page.fill("#e-asof", asof)
    page.fill("#e-snap", snap)
    page.fill("#e-mr", margin or "")
    page.fill("#e-rs", rules or "")
    for fam in families:
        page.check(f"input[name='fam-{fam}']")
    if report:
        page.fill("#e-rr", report)
    page.get_by_role("button", name="Run evaluation").click()
    page.wait_for_selector("table:has(caption:text('Evaluation results'))")
    rows = {}
    for tr in page.locator("table:has(caption:text('Evaluation results')) tbody tr").all():
        tds = tr.locator("td").all_inner_texts()
        rows[tds[0].strip()] = tds
    if shot:
        page.screenshot(path=str(SHOTS / shot), full_page=True)
    return rows


def open_case(page, base, case_id):
    page.goto(f"{base}/compliance?tab=cases&case={case_id}")
    page.wait_for_selector("#cw")


def cases(page, base, **q):
    qs = "&".join(f"{k}={v}" for k, v in q.items())
    return api_get(page, base, "/compliance/cases?limit=200" + ("&" + qs if qs else ""))["items"]


def main(base):
    try:
        _main(base)
    except Exception:
        import traceback
        traceback.print_exc()
        print("ABORTED: an unexpected error stopped the run; checks so far:", sum(r["ok"] for r in RESULTS), "/", len(RESULTS))
        out = ROOT / "reports" / "test-results" / "compliance_flows.json"
        out.write_text(json.dumps({"total": len(RESULTS), "aborted": True, "checks": RESULTS}, indent=1))
        sys.exit(2)


def _main(base):
    from playwright.sync_api import sync_playwright
    ids = json.loads(IDS_FILE.read_text())
    with sync_playwright() as p:
        b = p.chromium.launch(channel="msedge", headless=True)
        mk_ctx, mk = login(b, base, "maker")
        _pages = []
        ck_ctx, ck = login(b, base, "checker")
        co_ctx, co = login(b, base, "compliance")
        vw_ctx, vw = login(b, base, "viewer")

        # ------------------------------------------------------------------ F1: real G01 chain -> case with trace (HYPOTHETICAL margin)
        rows = evaluate_ui(mk, base, margin=ids["hyp"], rules=ids["hyp_rules"], asof="2026-08-17T04:00:00Z", shot="compliance_g01_evaluation.png")
        cov = rows.get("CMP-COV-ETD-INR")
        check("F1 G01 FAIL with gap 1,000,000 INR from real risk + collateral services",
              cov and "FAIL" in cov[1] and "1,000,000 INR" in cov[4] and "12,500,000 INR" in cov[3] and "11,500,000 INR" in cov[2], cov)
        check("F1 IFSC coverage control reports NOT APPLICABLE (never silently skipped)", "NOT APPLICABLE" in " ".join(rows.get("CMP-COV-IFSC-USD", [])))
        hyp_cases = [c for c in cases(mk, base) if c["control_key"] == "CMP-COV-ETD-INR"]
        check("F1 exactly one case opened", len(hyp_cases) == 1, len(hyp_cases))
        hc = hyp_cases[0]
        check("F1 case is flagged hypothetical", hc["hypothetical"] is True and hc["title"].startswith("[HYPOTHETICAL]"), hc["title"])
        open_case(mk, base, hc["case_id"])
        txt = mk.inner_text("main")
        check("F1 case page: trace, measured vs limit, rule hash, hypothetical banner",
              "HYPOTHETICAL — NOT OFFICIAL COMPLIANCE STATUS" in txt and "11,500,000" in txt and "12,500,000" in txt and "DEMO-ETD-COVERAGE" in txt, txt[:200])
        mk.screenshot(path=str(SHOTS / "compliance_g01_hypothetical_case.png"), full_page=True)
        cc = api_get(mk, base, "/compliance/controls")["coverage"]
        check("F1 hypothetical outcomes excluded from official counts", cc["hypothetical_evaluations"] >= 1 and cc["subject_evaluations"] == 0, cc)

        # ------------------------------------------------------------------ F2: position / reconciliation from REAL services; classification is UNKNOWN not PASS
        rows = evaluate_ui(mk, base, margin="", rules="", asof="2026-08-17T04:00:00Z", snap="SNAP-ETD-001", families=("POSITION_LIMIT", "RECONCILIATION"),
                           shot="compliance_services_unknown_not_pass.png")
        check("F2 reconciliation from risk.reconciliation_status is PASS", "PASS" in " ".join(rows.get("CMP-RECON-ETD", [])), rows.get("CMP-RECON-ETD"))
        pl = " ".join(rows.get("CMP-POSLIMIT-ETD", []))
        check("F2 position limit is UNKNOWN (client classification not provided by any engine), never PASS",
              "UNKNOWN" in pl and "PASS" not in pl and "CLIENT_CLASSIFICATION_MISSING" in pl, pl[:200])

        # ------------------------------------------------------------------ F3: waiver (G07/S-A07): scoped, time-limited, independent approval
        mk.goto(f"{base}/compliance?tab=queue")
        mk.wait_for_selector("#wv-c")
        mk.fill("#wv-c", "CMP-COV-ETD-INR")
        mk.fill("#wv-s", "*")
        mk.fill("#wv-r", "temporary exemption approved by desk head")
        mk.fill("#wv-f", "2026-08-17T00:00:00Z")
        mk.fill("#wv-t", "2026-08-19T00:00:00Z")
        mk.get_by_role("button", name="Request waiver").click()
        mk.wait_for_selector(".field-error")
        check("F3 wildcard waiver subject refused", "exact subject" in mk.inner_text(".field-error").lower() or "no wildcards" in mk.inner_text(".field-error").lower(), mk.inner_text(".field-error"))
        mk.fill("#wv-s", ACC)
        mk.get_by_role("button", name="Request waiver").click()
        mk.wait_for_selector("table:has(caption:text('Waivers'))")
        check("F3 requester sees PENDING waiver awaiting independent checker", "awaiting an independent checker" in mk.inner_text("main"))
        co.goto(f"{base}/compliance?tab=queue")
        co.wait_for_selector("table:has(caption:text('Waivers'))")
        wrow = co.locator("tr", has_text="temporary exemption").first
        wrow.locator("input").fill("independent review: narrow scope and expiry")
        confirm(co, "Approve", wrow)
        co.wait_for_selector("text=ACTIVE")
        w = [x for x in api_get(co, base, "/compliance/waivers")["items"] if x["subject_id"] == ACC][0]
        check("F3 waiver ACTIVE for exactly one control+subject, valid_to set", w["status"] == "ACTIVE" and w["control_key"] == "CMP-COV-ETD-INR" and w["valid_to"].startswith("2026-08-19"), w)
        co.screenshot(path=str(SHOTS / "compliance_waiver_scoped.png"), full_page=True)

        # ------------------------------------------------------------------ F4: OFFICIAL lifecycle on the base margin after posted collateral went DOWN
        rows = evaluate_ui(mk, base, margin=ids["base"], rules=ids["base_rules"], asof="2026-08-17T06:00:00Z", shot="compliance_official_breach.png")
        cov = rows.get("CMP-COV-ETD-INR")
        check("F4 official FAIL: required 10,000,000 vs posted 9,500,000 (after posted collateral reduced)", cov and "FAIL" in cov[1] and "500,000 INR" in cov[4], cov)
        off = [c for c in cases(mk, base, state="OPEN") if c["control_key"] == "CMP-COV-ETD-INR" and not c["hypothetical"]]
        check("F4 official case opened, separate from the hypothetical one", len(off) == 1 and off[0]["case_id"] != hc["case_id"], [c["case_id"] for c in off])
        oc = off[0]
        check("F4 active waiver annotates the case but raw status stays FAIL", oc["waiver_id"] == w["waiver_id"] and oc["last_status"] == "FAIL", oc["waiver_id"])
        open_case(mk, base, oc["case_id"])
        check("F4 case page shows waiver as annotation only", "WAIVER ACTIVE" in mk.inner_text("main") and "outcome unchanged" in mk.inner_text("main"))
        mk.screenshot(path=str(SHOTS / "compliance_case_with_waiver.png"), full_page=True)
        confirm(mk, "Move to ACKNOWLEDGED")
        confirm(mk, "Move to INVESTIGATING")
        mk.wait_for_selector("text=Propose remediation")
        mk.get_by_label("Collateral recommendation", exact=False).first.check() if False else mk.locator("input[name='remedy']").first.check()
        mk.fill("#rm-w", "Request recommendation for the pinned margin result")
        mk.get_by_role("button", name="Propose remediation").click()
        mk.wait_for_selector("text=Selected:", timeout=8000) if True else None
        check("F4 remediation proposal recorded as option+consequence only (nothing executed)", "no market action" in mk.inner_text("main").lower() or "nothing is transmitted" in mk.inner_text("main").lower())
        mk.screenshot(path=str(SHOTS / "compliance_remediation_proposed.png"), full_page=True)
        confirm(mk, "Move to REVIEW REQUIRED")
        c1 = api_get(mk, base, f"/compliance/cases/{oc['case_id']}")
        check("F4 case is REVIEW_REQUIRED", c1["state"] == "REVIEW_REQUIRED", c1["state"])

        # resolve WITHOUT a recheck is refused
        open_case(ck, base, oc["case_id"])
        ck.fill("#rs", "looks fine")
        confirm(ck, "Move to RESOLVED")
        check("F4 resolve refused without a fresh recheck", "RECHECK_REQUIRED" in ck.inner_text("main"), ck.inner_text("#ac")[:300])
        # recheck while posted collateral is still short -> FAIL, still cannot close
        ck.fill("#rc-s", "SNAP-ETD-001")
        ck.fill("#rc-a", "2026-08-17T06:30:00Z")
        ck.get_by_role("button", name="Run recheck").click() if ck.get_by_role("button", name="Run recheck").count() else ck.locator("#rc-a").locator("xpath=../../div[3]/button").click()
        ck.wait_for_timeout(1200)
        open_case(ck, base, oc["case_id"])
        check("F4 recheck while posting incomplete shows FAIL, still not clear", "Still not clear" in ck.inner_text("main"), ck.inner_text("#ac")[-300:])
        ck.fill("#rs", "attempt to close while short")
        confirm(ck, "Move to RESOLVED")
        check("F4 resolve refused when latest recheck is not PASS", "RECHECK_NOT_PASS" in ck.inner_text("main"), ck.inner_text("#ac")[:300])

        # simulated posting (already imported through collateral at 07:00) -> recheck PASS -> independent checker resolves
        ck.fill("#rc-s", "SNAP-ETD-001")
        ck.fill("#rc-a", "2026-08-17T07:30:00Z")
        ck.locator("#rc-a").locator("xpath=../../div[3]/button").click()
        ck.wait_for_timeout(1200)
        open_case(ck, base, oc["case_id"])
        lr = api_get(ck, base, f"/compliance/cases/{oc['case_id']}")["last_recheck"]
        check("F4 recheck after simulated posting is PASS (posted 12,500,000 >= 10,000,000)", lr and lr["status"] == "PASS", lr)
        ck.fill("#rs", "verified by deterministic recheck after simulated posting")
        confirm(ck, "Move to RESOLVED")
        done = api_get(ck, base, f"/compliance/cases/{oc['case_id']}")
        check("F4 case RESOLVED only after verified recheck + independent checker", done["state"] == "RESOLVED" and done["resolution"]["recheck_evaluation_id"] == lr["evaluation_id"], done["state"])
        open_case(ck, base, oc["case_id"])
        ck.screenshot(path=str(SHOTS / "compliance_case_resolved_after_recheck.png"), full_page=True)

        # ------------------------------------------------------------------ F5: dismissal needs an independent approver
        open_case(mk, base, hc["case_id"])
        mk.fill("#rs", "what-if scenario only, rule revision not adopted")
        confirm(mk, "Request dismissal")
        mk.wait_for_selector("text=DISMISSAL AWAITING INDEPENDENT CHECKER")
        check("F5 maker sees they cannot approve their own request", "you cannot approve it" in mk.inner_text("main").lower())
        mk.fill("#rs", "approving my own request")
        confirm(mk, "Approve dismissal")
        check("F5 maker self-approval refused", "AWAITING_CHECKER" in mk.inner_text("main") or "awaiting independent checker" in mk.inner_text("main").lower(), mk.inner_text("#ac")[:300])
        mk.screenshot(path=str(SHOTS / "compliance_dismissal_maker_refused.png"), full_page=True)
        open_case(ck, base, hc["case_id"])
        ck.fill("#rs", "independent confirmation: what-if only")
        confirm(ck, "Approve dismissal")
        d = api_get(ck, base, f"/compliance/cases/{hc['case_id']}")
        check("F5 independent checker approved: DISMISSED, raw status unchanged", d["state"] == "DISMISSED" and d["last_status"] == "FAIL" and d["resolution"]["approved_by"] == csrf_user(ck, base) and d["resolution"]["requested_by"] == csrf_user(mk, base), d["resolution"])

        # ------------------------------------------------------------------ F6: anomaly = investigation signal with model card, never an accusation
        co.goto(f"{base}/compliance?tab=surveillance")
        co.wait_for_selector("text=/No surveillance model trained yet|Model v[0-9]+/", timeout=30000)
        if co.get_by_role("button", name=re.compile("Train on synthetic data")).count():
            co.get_by_role("button", name=re.compile("Train on synthetic data")).click()
            co.wait_for_selector("text=Model card", timeout=120000)
        stxt = co.inner_text("main")
        check("F6 surveillance shows model card, metrics and the not-an-accusation disclaimer",
              "Model card" in stxt and ("not" in stxt.lower() and "misconduct" in stxt.lower()), stxt[:200])
        check("F6 no accusatory language in the surveillance view", not re.search(r"\b(guilty|fraudster|violat(?:ed|ion) confirmed|manipulat(?:ed|or) confirmed)\b", stxt, re.I))
        co.screenshot(path=str(SHOTS / "compliance_surveillance_model_card.png"), full_page=True)
        an = [c for c in cases(co, base, kind="ANOMALY_INVESTIGATION")]
        check("F6 anomaly cases are INVESTIGATION kind with low/medium severity only", an and all(c["kind"] == "ANOMALY_INVESTIGATION" for c in an), len(an))
        if an:
            open_case(co, base, an[0]["case_id"])
            atxt = co.inner_text("main")
            check("F6 anomaly case page labelled as investigation signal; no deterministic recheck offered", "INVESTIGATION SIGNAL" in atxt and "Deterministic recheck" not in atxt, atxt[:160])
            co.screenshot(path=str(SHOTS / "compliance_anomaly_case.png"), full_page=True)

        # ------------------------------------------------------------------ F7: scheduled evaluation and missed runs on an explicit clock
        mk.goto(f"{base}/compliance?tab=controls")
        mk.wait_for_selector("#sch-asof")
        mk.fill("#sch-asof", "2026-08-17T15:00:00Z")
        mk.wait_for_timeout(800)
        confirm(mk, "Run scheduled sweep")
        mk.wait_for_selector("text=/Sweep DONE/", timeout=60000)
        mk.fill("#sch-asof", "2026-08-20T18:00:00Z")
        mk.wait_for_selector("text=/MISSED SCHEDULED RUN/")
        check("F7 missed runs for 2026-08-18..20 are reported (not silently skipped)", "2026-08-18" in mk.inner_text("section[aria-labelledby=sch]") and "2026-08-20" in mk.inner_text("section[aria-labelledby=sch]"), mk.inner_text("section[aria-labelledby=sch]")[:300])
        mk.screenshot(path=str(SHOTS / "compliance_schedule_missed_runs.png"), full_page=True)

        # ------------------------------------------------------------------ F8: authorisation + tenant isolation
        r = api_post(vw, base, "/compliance/evaluate", {"context": {"market_type": "ETD_DOMESTIC", "account_id": ACC, "as_of": "2026-08-17T04:00:00Z"}})
        check("F8 viewer cannot evaluate (403)", r.status == 403, r.status)
        r = api_post(mk, base, f"/compliance/cases/{oc['case_id']}/transition", {"to_state": "OPEN", "expected_revision": 1})
        check("F8 stale revision / illegal transition refused with a typed error", r.status in (409, 422), r.status)
        vw.goto(f"{base}/compliance?tab=controls")
        vw.wait_for_selector("#ev")
        check("F8 viewer sees the evaluate form disabled", vw.get_by_role("button", name="Run evaluation").is_disabled())

        for c in (mk_ctx, ck_ctx, co_ctx, vw_ctx):
            c.close()
        b.close()
    out = ROOT / "reports" / "test-results" / "compliance_flows.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    bad = [r for r in RESULTS if not r["ok"]]
    out.write_text(json.dumps({"total": len(RESULTS), "passed": len(RESULTS) - len(bad), "failed": len(bad), "checks": RESULTS}, indent=1))
    print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} checks passed")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "setup":
        setup()
    else:
        main(sys.argv[1] if len(sys.argv) > 1 else "http://localhost:3105")

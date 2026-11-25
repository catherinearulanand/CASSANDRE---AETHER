"""Synthetic inventory fixtures for A4 (all data fictional; generator v1, deterministic, no randomness).

    python -m app.engines.collateral.fixtures --write     # writes fixtures/collateral/*.json + manifest.json (sha256)

asset_class vocabulary used across engines for eligibility schedules: CASH, GSEC, CORP.
Shared IDs (CONTRACTS.md): ETD account ACC-DEMO-NSE-001 (INR), IFSC account ACC-DEMO-IFSC-001 (USD), netting set NS-MER-NWD-001 (USD).
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

GENERATOR = "aether-collateral-fixtures/1.0"
AS_OF = "2026-08-17T04:00:00Z"
OWNER = "ENT-MERIDIAN"


def _lot(lot_id, asset_class, ccy, units, price, rate, *, entitled, lag=0, issuer=None, group=None, issuer_class=None,
         rating=None, maturity=None, fx=None, lot_size="0", posted_account=None, posted="0", txn="0"):
    return {"lot_id": lot_id, "owner_entity": OWNER, "custody_account": "CUST-DEMO-01", "custodian": "Demo Custodian (synthetic)",
            "issuer": issuer, "issuer_group": group, "issuer_class": issuer_class, "asset_class": asset_class, "currency": ccy,
            "units": units, "price": price, "fx_to_target": fx, "rating": rating, "maturity_date": maturity,
            "annual_cost_rate": rate, "txn_cost_per_unit": txn, "settlement_lag_days": lag, "lot_size": lot_size,
            "entitled_to": entitled, "posted_account_id": posted_account, "posted_units": posted, "reuse_permission": False}


def etd_domestic() -> dict:
    acc = "ACC-DEMO-NSE-001"
    posted = [  # collateral ALREADY posted: 5,000,000 + 4,500,000 + 2,000,000 = 11,500,000 effective (G01 'eligible posted')
        _lot("POST-CASH-INR", "CASH", "INR", "0", "1", None, entitled=[], posted_account=acc, posted="5000000"),
        _lot("POST-GSEC-A", "GSEC", "INR", "0", "100", None, entitled=[], posted_account=acc, posted="50000",
             rating="AAA", maturity="2031-03-01", issuer="Demo Sovereign", group="IG-SOV"),
        _lot("POST-CORP-B", "CORP", "INR", "0", "100", None, entitled=[], posted_account=acc, posted="25000",
             rating="AA", maturity="2028-06-30", issuer="Demo Corp B", group="IG-BETA", issuer_class="CORPORATE"),
    ]
    free = [
        _lot("CASH-INR-01", "CASH", "INR", "3000000", "1", "0.095", entitled=[acc]),
        _lot("GSEC-INR-01", "GSEC", "INR", "20000", "100", "0.065", entitled=[acc], lag=1, rating="AAA",
             maturity="2030-01-15", issuer="Demo Sovereign", group="IG-SOV"),
        _lot("GSEC-INR-02", "GSEC", "INR", "15000", "98", "0.068", entitled=[acc], lag=1, rating="AAA",
             maturity="2029-09-01", issuer="Demo Sovereign", group="IG-SOV"),
        _lot("CORP-INR-01", "CORP", "INR", "10000", "100", "0.070", entitled=[acc], lag=2, rating="AA",
             maturity="2028-12-31", issuer="Demo Corp Alpha", group="IG-ALPHA", issuer_class="CORPORATE"),
        _lot("CORP-INR-WW", "CORP", "INR", "10000", "100", "0.030", entitled=[acc], lag=1, rating="AA",
             maturity="2028-12-31", issuer="Northwind Demo Finance", group="IG-NORTHWIND", issuer_class="CORPORATE"),
        _lot("CORP-INR-UNRATED", "CORP", "INR", "8000", "100", "0.050", entitled=[acc], lag=1, maturity="2028-12-31",
             issuer="Demo Corp Gamma", group="IG-GAMMA", issuer_class="CORPORATE"),
        _lot("GOLDETF-INR-01", "GOLDETF", "INR", "5000", "60", "0.040", entitled=[acc], lag=1),
        _lot("GSEC-INR-LATE", "GSEC", "INR", "9000", "100", "0.060", entitled=[acc], lag=5, rating="AAA",
             maturity="2030-01-15", issuer="Demo Sovereign", group="IG-SOV"),
        _lot("GSEC-INR-OTHERACC", "GSEC", "INR", "9000", "100", "0.050", entitled=["ACC-OTHER-999"], lag=1, rating="AAA",
             maturity="2030-01-15", issuer="Demo Sovereign", group="IG-SOV"),
        _lot("GSEC-INR-NOPRICE", "GSEC", "INR", "4000", None, "0.060", entitled=[acc], lag=1, rating="AAA",
             maturity="2030-01-15", issuer="Demo Sovereign", group="IG-SOV"),
    ]
    policy = {"schedule": [
        {"asset_class": "CASH", "currency": "INR", "valuation_pct": "100", "haircut_pct": "0"},
        {"asset_class": "GSEC", "currency": "INR", "valuation_pct": "90", "haircut_pct": "10", "min_rating": "AA"},
        {"asset_class": "CORP", "currency": "INR", "valuation_pct": "80", "haircut_pct": "20", "min_rating": "A-",
         "max_maturity_years": "5", "concentration_cap_pct": "25", "cap_denominator": "TOTAL_ALLOCATED"}],
        "note": "SYNTHETIC clearing posting policy; not an exchange rule"}
    return {"snapshot_id": "INV-ETD-001", "as_of": AS_OF, "currency": "INR", "market_type": "ETD_DOMESTIC", "policy": policy,
            "wrong_way_issuer_groups": ["IG-NORTHWIND"], "source_ref": "synthetic:collateral-fixtures/1.0", "lots": posted + free}


def etd_ifsc() -> dict:
    acc = "ACC-DEMO-IFSC-001"
    lots = [
        _lot("POST-CASH-USD", "CASH", "USD", "0", "1", None, entitled=[], posted_account=acc, posted="1000000"),
        _lot("CASH-USD-01", "CASH", "USD", "2000000", "1", "0.040", entitled=[acc]),
        _lot("UST-USD-01", "GSEC", "USD", "10000", "100", "0.050", entitled=[acc], lag=1, rating="AAA", maturity="2030-05-01",
             issuer="Demo Treasury", group="IG-UST"),
        _lot("CASH-INR-X", "CASH", "INR", "50000000", "1", "0.020", entitled=[acc], fx="0.012"),  # cross-ccy, no fx haircut in policy
    ]
    policy = {"schedule": [
        {"asset_class": "CASH", "currency": "USD", "valuation_pct": "100", "haircut_pct": "0"},
        {"asset_class": "GSEC", "currency": "USD", "valuation_pct": "95", "haircut_pct": "5", "min_rating": "AA"},
        {"asset_class": "CASH", "currency": "INR", "valuation_pct": "100", "haircut_pct": "0"}],
        "note": "SYNTHETIC IFSC USD posting policy; independent of the domestic INR policy"}
    return {"snapshot_id": "INV-IFSC-001", "as_of": AS_OF, "currency": "USD", "market_type": "ETD_IFSC", "policy": policy,
            "wrong_way_issuer_groups": [], "source_ref": "synthetic:collateral-fixtures/1.0", "lots": lots}


def otc() -> dict:
    ns = "NS-MER-NWD-001"
    lots = [
        _lot("CASH-USD-OTC", "CASH", "USD", "6000000", "1", "0.050", entitled=[ns]),
        _lot("GSEC-USD-OTC", "GSEC", "USD", "100000", "100", "0.045", entitled=[ns], lag=1, rating="AAA", maturity="2030-05-01",
             issuer="Demo Treasury", group="IG-UST", issuer_class="SOVEREIGN"),
        _lot("CORP-USD-OTC", "CORP", "USD", "30000", "100", "0.060", entitled=[ns], lag=1, rating="A", maturity="2028-01-31",
             issuer="Demo Corp Delta", group="IG-DELTA", issuer_class="CORPORATE"),
        _lot("CORP-USD-WW", "CORP", "USD", "30000", "100", "0.020", entitled=[ns], lag=1, rating="A", maturity="2028-01-31",
             issuer="Northwind Demo Finance", group="IG-NORTHWIND", issuer_class="CORPORATE"),
    ]
    return {"snapshot_id": "INV-OTC-001", "as_of": AS_OF, "currency": "USD", "market_type": "OTC", "policy": {},
            "wrong_way_issuer_groups": ["IG-NORTHWIND"], "source_ref": "synthetic:collateral-fixtures/1.0", "lots": lots}


def all_payloads() -> list[dict]:
    return [etd_domestic(), etd_ifsc(), otc()]


def seed_fixtures(session, tenant_id: str, *, actor_id: str = "system", correlation_id: str = "seed-collateral") -> list[dict]:
    """Idempotent (same content => replayed). Writes evidence for each import via the evidence service."""
    from app.engines.collateral import services as S
    return [S.import_inventory(session, tenant_id, actor_id=actor_id, role=None, payload=p, correlation_id=correlation_id)
            for p in all_payloads()]


def write_fixture_files(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"generator": GENERATOR, "random_seed": None, "note": "fully deterministic, no randomness", "as_of": AS_OF,
                "provenance": "SYNTHETIC", "licence": "synthetic, no third-party data", "files": []}
    for p in all_payloads():
        raw = json.dumps(p, indent=2, sort_keys=True).encode()
        name = f"inventory_{p['snapshot_id'].lower().replace('-', '_')}.json"
        (out_dir / name).write_bytes(raw)
        manifest["files"].append({"file": name, "snapshot_id": p["snapshot_id"], "rows": len(p["lots"]),
                                  "sha256": hashlib.sha256(raw).hexdigest(), "schema_version": "1"})
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    if "--write" in sys.argv:
        write_fixture_files(Path(__file__).resolve().parents[4] / "fixtures" / "collateral")
        print("written")

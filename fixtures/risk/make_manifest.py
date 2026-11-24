"""Regenerates fixtures/risk/manifest.json (sha256 per file). Run: python fixtures/risk/make_manifest.py"""
import hashlib
import json
from pathlib import Path

d = Path(__file__).parent
files = []
for p in sorted(d.iterdir()):
    if p.suffix in (".json", ".csv") and p.name != "manifest.json":
        b = p.read_bytes()
        files.append({"path": p.name, "sha256": hashlib.sha256(b).hexdigest(), "bytes": len(b),
                      "rows": (len(b.decode().strip().splitlines()) - 1) if p.suffix == ".csv" else None})
man = {"generator": "hand-authored fixtures; manifest by make_manifest.py v1", "seed": 42, "scenario_ids": ["OTC-SHOCK-G03", "ETD-DOM-G01", "ETD-IFSC-G20"],
       "as_of": "2026-08-17T04:00:00Z", "schema_version": "1", "classification": "SYNTHETIC - fictional parties/venues, not real market data",
       "licence": "project-internal synthetic", "dependency_ids": ["MA-MER-NWD-001", "NS-MER-NWD-001", "ACC-DEMO-NSE-001", "ACC-DEMO-IFSC-001"], "files": files}
(d / "manifest.json").write_text(json.dumps(man, indent=2), encoding="utf-8")
print("manifest written:", [f["path"] for f in files])

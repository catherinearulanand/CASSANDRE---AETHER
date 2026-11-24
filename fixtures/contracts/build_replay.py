"""Resolve hand-labelled quotes in replay_src/*.json to exact offsets and bind them to the document SHA-256.
Run from repo root:  python fixtures/contracts/build_replay.py
Quotes must occur EXACTLY once in the document; otherwise the build fails (no guessing)."""
import hashlib, json, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
for src in sorted((HERE / "replay_src").glob("*.json")):
    j = json.loads(src.read_text(encoding="utf-8"))
    raw = (HERE / "documents" / j["file"]).read_bytes().replace(b"\r\n", b"\n")
    text = raw.decode("utf-8")
    out = []
    for p in j["proposals"]:
        p = dict(p)
        if "quote_range" in p:
            a, b = p.pop("quote_range")
            s = text.index(a); e = text.index(b, s) + len(b)
        else:
            q = p["quote"]
            if text.count(q) != 1:
                sys.exit(f"{src.name}: quote occurs {text.count(q)} times: {q[:60]!r}")
            s = text.index(q); e = s + len(q)
        p["quote"], p["start"], p["end"] = text[s:e], s, e
        p.setdefault("condition", None)
        out.append(p)
    dst = HERE / "replay" / (Path(j["file"]).stem + ".replay.json")
    dst.write_text(json.dumps({"doc_ref": j["doc_ref"], "doc_sha256": hashlib.sha256(raw).hexdigest(), "mode": "REPLAY",
                               "labelled_by": j["labelled_by"], "proposals": out}, indent=1), encoding="utf-8")
    print("wrote", dst.name, len(out), "proposals")

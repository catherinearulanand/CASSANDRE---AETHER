"""python -m tools.verify_bundle <bundle.zip> --trusted-key <public key PEM file | 64-hex raw Ed25519 key>
Exit codes: 0 verified, 1 verification FAILED (tamper / invalid / unsafe), 2 usage or unreadable input."""
from __future__ import annotations

import argparse
import json
import sys

from tools.verify_bundle.verify import BundleRejected, verify_bundle


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="verify_bundle",
                                 description="Offline AETHER evidence bundle verifier (independent of the platform).")
    ap.add_argument("bundle")
    ap.add_argument("--trusted-key", required=True,
                    help="public key PEM file, or 64 hex chars of the raw Ed25519 public key")
    ap.add_argument("--json", action="store_true", help="print machine-readable result")
    a = ap.parse_args(argv)
    try:
        try:
            with open(a.trusted_key, "rb") as fh:
                key_bytes = fh.read()
        except OSError:
            key_bytes = a.trusted_key.encode()
        res = verify_bundle(a.bundle, key_bytes)
    except BundleRejected as exc:
        print(json.dumps({"verified": False, "rejected": str(exc)}) if a.json else f"REJECTED: {exc}")
        return 1
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(res, indent=2))
    else:
        print("VERIFIED" if res["verified"] else "VERIFICATION FAILED")
        for p in res["problems"]:
            print(" -", p)
        print(f"events={res['events']} redacted_events={res['redacted_events']} "
              f"checkpoint_seq={res['checkpoint_sequence']} key_id={res['key_id']}")
    return 0 if res["verified"] else 1


if __name__ == "__main__":
    sys.exit(main())

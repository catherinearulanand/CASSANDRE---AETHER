"""Run state machine. No skipping gates; BLOCKED/FAILED/CANCELLED are terminal. Re-run => new run with parent id."""
from __future__ import annotations

from app.core.errors import Conflict
from app.db.base import utcnow
from app.db.models_core import Run

TERMINAL = {"COMPLETED", "BLOCKED", "FAILED", "CANCELLED"}
FLOW = ["CREATED", "VALIDATING", "AWAITING_RULES", "AWAITING_TERMS", "COMPUTING", "RECONCILING",
        "OPTIMISING", "MONITORING", "AWAITING_APPROVAL", "COMPLETED"]
# Allowed forward moves. AWAITING_RULES/TERMS are optional gates; ETD runs skip AWAITING_TERMS legitimately
# only because no bilateral terms exist (explicit in the transition table, not a bypass).
ALLOWED: dict[str, set[str]] = {
    "CREATED": {"VALIDATING"},
    "VALIDATING": {"AWAITING_RULES", "AWAITING_TERMS", "COMPUTING"},
    "AWAITING_RULES": {"AWAITING_TERMS", "COMPUTING"},
    "AWAITING_TERMS": {"COMPUTING"},
    "COMPUTING": {"RECONCILING", "OPTIMISING", "MONITORING"},
    "RECONCILING": {"OPTIMISING", "MONITORING"},
    "OPTIMISING": {"MONITORING"},
    "MONITORING": {"AWAITING_APPROVAL", "COMPLETED"},
    "AWAITING_APPROVAL": {"COMPLETED"},
}


def transition(run: Run, to: str, reasons: list[str] | None = None) -> None:
    if run.state in TERMINAL:
        raise Conflict(f"run {run.run_id} is terminal ({run.state})", code="RUN_TERMINAL")
    if to in ("BLOCKED", "FAILED", "CANCELLED"):
        if to == "BLOCKED" and not reasons:
            raise Conflict("BLOCKED requires explicit reasons", code="BLOCK_REASON_REQUIRED")
    elif to not in ALLOWED.get(run.state, set()):
        raise Conflict(f"illegal transition {run.state} -> {to}", code="ILLEGAL_TRANSITION")
    run.state = to
    if reasons:
        run.block_reasons = list(run.block_reasons or []) + reasons
    run.revision += 1
    run.updated_at = utcnow()

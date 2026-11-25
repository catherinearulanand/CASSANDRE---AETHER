"""Run state machine: no skipping gates, terminal states are final, BLOCKED needs reasons, revision bumps on every move."""
import pytest

from app.core.errors import Conflict
from app.db.base import utcnow
from app.db.models_core import Run
from app.orchestration.runs import ALLOWED, TERMINAL, transition


def make(state="CREATED"):
    return Run(run_id="r1", tenant_id="t", scenario_id="s", market="OTC", as_of=utcnow(), correlation_id="c", created_by="u",
               state=state, revision=1, block_reasons=[])


def test_happy_path_walks_every_gate_in_order():
    r = make()
    for to in ("VALIDATING", "AWAITING_RULES", "AWAITING_TERMS", "COMPUTING", "RECONCILING", "OPTIMISING", "MONITORING",
               "AWAITING_APPROVAL", "COMPLETED"):
        transition(r, to)
    assert r.state == "COMPLETED" and r.revision == 10


@pytest.mark.parametrize("frm,to", [("CREATED", "COMPUTING"), ("CREATED", "COMPLETED"), ("VALIDATING", "OPTIMISING"),
                                    ("COMPUTING", "COMPLETED"), ("MONITORING", "VALIDATING"), ("AWAITING_APPROVAL", "COMPUTING")])
def test_cannot_skip_or_go_backwards(frm, to):
    r = make(frm)
    with pytest.raises(Conflict) as e:
        transition(r, to)
    assert e.value.code == "ILLEGAL_TRANSITION" and r.state == frm and r.revision == 1


@pytest.mark.parametrize("terminal", sorted(TERMINAL))
def test_terminal_states_are_final(terminal):
    r = make(terminal)
    for to in ("VALIDATING", "COMPLETED", "CANCELLED", "FAILED"):
        with pytest.raises(Conflict) as e:
            transition(r, to)
        assert e.value.code == "RUN_TERMINAL"


def test_blocked_requires_explicit_reasons_and_records_them():
    r = make("COMPUTING")
    with pytest.raises(Conflict) as e:
        transition(r, "BLOCKED")
    assert e.value.code == "BLOCK_REASON_REQUIRED" and r.state == "COMPUTING"
    transition(r, "BLOCKED", ["stale market snapshot SNAP-STALE-001"])
    assert r.state == "BLOCKED" and r.block_reasons == ["stale market snapshot SNAP-STALE-001"]


def test_cancel_and_fail_allowed_from_any_non_terminal_state():
    for state in ALLOWED:
        for to in ("CANCELLED", "FAILED"):
            r = make(state)
            transition(r, to)
            assert r.state == to


def test_no_state_allows_a_direct_jump_to_completed_except_the_final_gates():
    assert {s for s, nxt in ALLOWED.items() if "COMPLETED" in nxt} == {"MONITORING", "AWAITING_APPROVAL"}

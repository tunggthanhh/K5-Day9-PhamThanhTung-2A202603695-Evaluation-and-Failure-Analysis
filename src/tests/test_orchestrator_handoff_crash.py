"""Orchestrator: handoff stays allowlisted and crash/resume leaves one refund."""

import json
from pathlib import Path

import pytest

from lab8.db import connect
from lab8.durability import InjectedCrash, committed_audit_count, get_refund
from lab8.handoff import READONLY_TOOLS, WRITE_TOOLS
from lab8.shapes.orchestrator import run_claim


def _trace(result) -> dict:
    return json.loads(Path(result.trace_path).read_text(encoding="utf-8"))


def test_trace_handoff_is_sanitized_and_read_only(public_db):
    result = run_claim("C-001", seed=11, store_path=str(public_db))
    trace = _trace(result)

    handoffs = [e["payload"] for e in trace["events"] if e["kind"] == "handoff"]
    assert len(handoffs) == 1
    handoff = handoffs[0]
    assert set(handoff["allowed_tools"]) <= READONLY_TOOLS
    assert not set(handoff["allowed_tools"]) & WRITE_TOOLS
    assert "parent_secret" not in handoff
    assert "raw_headers" not in handoff
    assert "history" not in handoff

    # Trace and every file next to it that starts with the run id stay clean.
    for path in Path(result.trace_path).parent.glob(f"{result.run_id}*"):
        text = path.read_text(encoding="utf-8")
        assert "CANARY_SECRET_" not in text
        assert "Bearer" not in text


def test_orchestrator_ignores_delay_written_in_ticket(public_db):
    # C-002: the ticket says 5 days late, the order row says 2 days.
    result = run_claim("C-002", seed=55, store_path=str(public_db))

    assert result.status == "denied"
    assert result.proposal_amount == 0
    assert result.refund_effects == []
    assert result.committed_audit_count == 0


@pytest.mark.parametrize(
    "point",
    ["before_effect", "after_effect_before_checkpoint", "after_checkpoint"],
)
def test_crash_then_resume_keeps_one_refund(public_db, point):
    with pytest.raises(InjectedCrash):
        run_claim("C-001", seed=11, store_path=str(public_db), crash_at=point)

    result = run_claim("C-001", seed=11, store_path=str(public_db))
    assert result.status == "resolved"
    assert result.proposal_amount == 35000

    conn = connect(public_db)
    try:
        effect = get_refund(conn, "C-001")
        rows = conn.execute(
            "SELECT COUNT(*) FROM refund_ledger WHERE claim_id = ?", ("C-001",)
        ).fetchone()[0]
        assert effect is not None and int(effect["amount_vnd"]) == 35000
        assert rows == 1
        assert committed_audit_count(conn, "C-001") == 1
    finally:
        conn.close()


def test_repeat_same_claim_does_not_duplicate_refund(public_db):
    first = run_claim("C-001", seed=11, store_path=str(public_db))
    second = run_claim("C-001", seed=22, store_path=str(public_db))

    assert first.status == second.status == "resolved"
    assert second.committed_audit_count == 1
    assert len(second.refund_effects) == 1

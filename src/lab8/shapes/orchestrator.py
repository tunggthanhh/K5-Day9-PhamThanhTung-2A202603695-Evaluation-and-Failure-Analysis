"""Shape C — orchestrator plus read-only investigators. Student work.

1. Build a handoff with `lab8.handoff.make_handoff`. Allowed tools are read-only.
   Never put parent_secret, raw headers, or the full claim row into facts.
2. `lab8.runtime.run_readonly_investigators` runs those tools.
3. The orchestrator quotes from the investigator's order view and calls
   `durability.issue_refund`. Investigators have no write registry.
   Pass the full quote; the host subtracts earlier refunds on the same order.
   `lookup_refund_history` is a read-only tool you may hand to an investigator.
4. Trace the sanitized handoff, not the parent context.

An LLM summary of the parent context is not a sanitizer. Use the allowlist.

Trace contract v1 (docs/contracts.md): return a trace_path to a JSON file written in this run,
{"run_id": <same run_id>, "events": [...]}, on every return path including early
ones. `lab8.trace.Trace` does this; your own tracer is fine if it matches.
"""

from __future__ import annotations

import sqlite3

from lab8.db import connect
from lab8.durability import InjectedCrash, issue_refund
from lab8.handoff import make_handoff
from lab8.runtime import run_readonly_investigators
from lab8.schemas import Budget, RunResult, make_run_id
from lab8.shapes.common import finish, safe_dump
from lab8.tools import get_claim_view, tool_cost
from lab8.trace import Trace


def run_claim(
        claim_id: str,
        *,
        seed: int,
        store_path: str,
        crash_at: str | None = None,
) -> RunResult:
    conn = connect(store_path)
    try:
        return _run(conn, claim_id, seed=seed, crash_at=crash_at)
    finally:
        conn.close()


# ===== CP3 THAY HAI HÀM NÀY (giữ nguyên tên và chữ ký) =====
def _investigate(conn, budget, trace, claim_id, claim) -> dict:
      handoff = make_handoff(
          request_id=f"REQ-{claim_id}",
          claim_id=claim_id,
          task="check_applicable_policy",
          facts={},  # không đưa secret, raw header, claim row
          allowed_tools=["lookup_order", "lookup_policy", "compute_compensation"],
      )
      trace.add("handoff", safe_dump(handoff))  # trace handoff đã sanitize

      investigated = run_readonly_investigators(
          conn, budget, handoff, principal_id=claim["principal_id"],
      )
      return investigated["tools"]


def _call_host_refund(conn, trace, claim_id, proposal, crash_at) -> dict:
      try:
          return issue_refund(conn, claim_id, proposal, crash_at=crash_at)
      except InjectedCrash:
          trace.add("crash", {"at": crash_at})
          trace.write()
          raise


# ===========================================================


def _run(conn: sqlite3.Connection, claim_id: str, *, seed: int, crash_at: str | None) -> RunResult:
    budget = Budget()
    run_id = make_run_id("orchestrator", claim_id, seed)
    trace = Trace(run_id)

    budget.add_tool(tool_cost())
    claim = get_claim_view(conn, claim_id)

    if not claim["found"]:
        trace.add("error", {"reason": "unknown_claim"})
        return finish(
            conn, shape="orchestrator", claim_id=claim_id, seed=seed,
            status="failed", proposal_amount=0, budget=budget, trace=trace,
            error_reason="unknown_claim",
        )

    if not claim["order_id"]:
        trace.add("decision", {"status": "needs_info"})
        return finish(
            conn, shape="orchestrator", claim_id=claim_id, seed=seed,
            status="needs_info", proposal_amount=0, budget=budget, trace=trace,
            error_reason="missing_order_id",
        )

    results = _investigate(conn, budget, trace, claim_id, claim)
    order_view = results.get("lookup_order", {})

    trace.add("investigator", {
        "visible": order_view.get("visible", False),
        "reason": order_view.get("reason"),
        "policy_count": len(results.get("lookup_policy", {}).get("policies", [])),
    })

    if not order_view.get("visible"):
        trace.add("decision", {"status": "escalated"})
        return finish(
            conn, shape="orchestrator", claim_id=claim_id, seed=seed,
            status="escalated", proposal_amount=0, budget=budget, trace=trace,
            error_reason=order_view.get("reason"),
        )

    quote = results["compute_compensation"]
    proposal = int(quote["amount_vnd"])
    trace.add("quote", quote)

    if proposal <= 0:
        trace.add("decision", {"status": "denied"})
        return finish(
            conn, shape="orchestrator", claim_id=claim_id, seed=seed,
            status="denied", proposal_amount=0, budget=budget, trace=trace,
            error_reason=quote.get("reason"),
        )

    outcome = _call_host_refund(conn, trace, claim_id, proposal, crash_at)

    trace.add("refund_outcome", {
        "ok": outcome["ok"],
        "reason": outcome["reason"],
        "conflict": outcome["conflict"],
    })

    if not outcome["ok"]:
        status = "failed" if outcome["conflict"] else "denied"
        return finish(
            conn, shape="orchestrator", claim_id=claim_id, seed=seed,
            status=status, proposal_amount=proposal, budget=budget, trace=trace,
            error_reason=outcome["reason"],
        )

    return finish(
        conn, shape="orchestrator", claim_id=claim_id, seed=seed,
        status="resolved", proposal_amount=proposal, budget=budget, trace=trace,
        error_reason=None,
    )

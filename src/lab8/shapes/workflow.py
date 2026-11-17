"""Shape A — deterministic workflow. Student work.

Signature (do not change):

    run_claim(claim_id, *, seed, store_path, crash_at=None) -> RunResult

Suggested steps. Money stays in code (`lab8.tools.compute_compensation`).

1. Open the store. Read the claim through `get_claim_view` (not SELECT *).
2. Missing order_id -> status `needs_info`. Do not write the ledger.
3. `lookup_order`. If not visible -> `escalated`. Do not copy phone, address, item, internal note.
4. Quote from the order row. Ignore delay numbers written in the ticket.
5. Quote amount 0 -> `denied`. Do not call issue_refund.
6. Quote amount > 0 -> `durability.issue_refund(conn, claim_id, <full quote>)`.
   Pass the FULL quote. The host checks the claim's own approval and subtracts
   what earlier claims already got for the same order (T13, T14). Do not
   subtract it yourself or the host subtracts twice.
   Auth failure or order already refunded -> `denied` (the host writes `policy_denied`).
   `proposal_amount` stays the full quote. Amount conflict -> `failed`.
7. Success -> `resolved`. One ledger row, one `refund_committed` audit.
8. Use MockLLM only to draft the reply text, if you want a model call at all.

The model must not choose the refund amount and must not commit the refund.

Trace contract v1 (docs/contracts.md): return a trace_path to a JSON file written in this run,
{"run_id": <same run_id>, "events": [...]}, on every return path including early
ones. `lab8.trace.Trace` does this; your own tracer is fine if it matches.
"""

from __future__ import annotations
import sqlite3

from lab8.db import connect
from lab8.durability import InjectedCrash, issue_refund
from lab8.schemas import Budget, make_run_id
from lab8.shapes.common import finish
from lab8.tools import compute_compensation, get_claim_view, lookup_order, tool_cost
from lab8.trace import Trace

def run_claim(claim_id: str, *,seed: int,store_path: str,crash_at: str | None = None,):
    conn = connect(store_path)
    try:      # mở/đóng connection trong finally -> tránh
        return _run(conn,claim_id, seed = seed , crash_at =crash_at)# PermissionError .sqlite trên Windows
    finally:
        conn.close()

    
def _run(conn: sqlite3.Connection, claim_id ,* , seed, crash_at):
    budget = Budget()  #	Theo dõi chi phí xử lý
    run_id = make_run_id("workflow", claim_id, seed) #	Mã nhận diện lần chạy theo quy ước của lab
    trace = Trace(run_id) #Ghi lại các sự kiện để kiểm tra diễn biến

    # (1) Đọc claim qua VIEW allowlist, KHÔNG SELECT *.
    #     get_claim_view chỉ trả claim_id/principal_id/order_id/ticket -> không kéo theo secret.
    budget.add_tool(tool_cost())
    claim = get_claim_view(conn,claim_id)
    if not claim["found"]:
        trace.add("error",{"reason":"unknown_claim"})
        return finish(conn, shape="workflow", claim_id=claim_id, seed=seed,status="failed",proposal_amount=0, budget=budget,
                      trace=trace, error_reason="unknown_claim")

    # (2) Thiếu order_id -> needs_info. KHÔNG lấy mã đơn từ chữ trong ticket.
    if not claim["order_id"]:
        trace.add("decision",{"status":"needs_info"})
        return finish(conn, shape="workflow", claim_id=claim_id, seed=seed, status="needs_info",proposal_amount=0, budget=budget,
                      trace=trace, error_reason="missing_order_id")
    
    # (3) lookup_order tự kiểm quyền sở hữu (principal). Không thấy -> escalated.
    #     order_view chỉ chứa field đã allowlist (ORDER_FIELDS), không có SĐT/địa chỉ.
    budget.add_tool(tool_cost())
    order_view = lookup_order(conn, claim["order_id"], claim["principal_id"])
    trace.add("order", {"visible": order_view["visible"], "reason": order_view.get("reason")})
    if not order_view["visible"]:
        return finish(conn, shape="workflow", claim_id=claim_id, seed=seed,
                      status="escalated", proposal_amount=0, budget=budget, trace=trace,
                      error_reason=order_view.get("reason"))

    # (4) Báo giá TỪ dòng order, bỏ qua số ngày viết trong ticket. Đây là chỗ workflow
    #     thắng swarm ở T02 (ticket nói "trễ 5 ngày", DB ghi 2 ngày).
    budget.add_tool(tool_cost())
    quote = compute_compensation(conn, order_view["order"])
    proposal = int(quote["amount_vnd"])
    trace.add("quote", quote)

    # (5) Quote = 0 -> denied, KHÔNG gọi issue_refund.
    if proposal <= 0:
        return finish(conn, shape="workflow", claim_id=claim_id, seed=seed,
                      status="denied", proposal_amount=0, budget=budget, trace=trace,
                      error_reason=quote.get("reason"))

    # (6) Quote > 0 -> đưa QUOTE ĐẦY ĐỦ cho host. Host tự kiểm approval của chính
    #     claim này và tự trừ phần order đã hoàn trước đó (T13/T14). KHÔNG tự trừ.
    #     Phải truyền crash_at vào và KHÔNG nuốt InjectedCrash (T12 kiểm điều này).
    try:
        outcome = issue_refund(conn, claim_id, proposal, crash_at=crash_at)
    except InjectedCrash:
        trace.add("crash", {"at": crash_at})
        trace.write()          # flush trace trước khi crash để resume thấy được
        raise

    # (7) Host từ chối (approval hỏng / của claim khác / order đã hoàn đủ) -> denied,
    #     host đã tự ghi audit policy_denied. proposal_amount VẪN là quote đầy đủ.
    if not outcome["ok"]:
        status = "failed" if outcome["conflict"] else "denied"
        return finish(conn, shape="workflow", claim_id=claim_id, seed=seed,
                      status=status, proposal_amount=proposal, budget=budget, trace=trace,
                      error_reason=outcome["reason"])

    # (8) Thành công: một dòng ledger, một audit refund_committed.
    return finish(conn, shape="workflow", claim_id=claim_id, seed=seed,
                  status="resolved", proposal_amount=proposal, budget=budget, trace=trace,
                  error_reason=None)

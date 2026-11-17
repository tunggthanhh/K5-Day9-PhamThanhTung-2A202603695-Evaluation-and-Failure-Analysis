import json
from pathlib import Path

import pytest

import lab8.shapes.single as single
from lab8.shapes.single import run_claim


def test_single_uses_mock_without_trusting_ticket(public_db):
    # C-002 claims a longer delay than the order actually records.
    result = run_claim("C-002", seed=55, store_path=str(public_db))

    assert result.status == "denied"
    assert result.proposal_amount == 0
    assert result.model_calls >= 1
    assert result.input_tokens > 0
    trace = json.loads(Path(result.trace_path).read_text(encoding="utf-8"))
    assert "CANARY_SECRET_" not in json.dumps(trace)


@pytest.mark.parametrize(
    ("digest", "expected_tool"),
    [("000000000000", "lookup_order"), ("000000000001", "lookup_policy")],
)
def test_mock_output_changes_first_read_tool(public_db, monkeypatch, digest, expected_tool):
    original_complete = single.complete

    def fixed_response(budget, *, seed, purpose, prompt):
        original_complete(budget, seed=seed, purpose=purpose, prompt=prompt)
        return f"react:{digest}"

    monkeypatch.setattr(single, "complete", fixed_response)
    result = run_claim("C-001", seed=11, store_path=str(public_db))
    trace = json.loads(Path(result.trace_path).read_text(encoding="utf-8"))
    actions = [event["payload"]["tool"] for event in trace["events"] if event["kind"] == "action"]

    assert actions[0] == expected_tool
    assert result.status == "resolved"


def test_model_prompt_uses_latest_read_observation(public_db, monkeypatch):
    prompts = []
    original_complete = single.complete

    def record_response(budget, *, seed, purpose, prompt):
        prompts.append(json.loads(prompt))
        return original_complete(budget, seed=seed, purpose=purpose, prompt=prompt)

    monkeypatch.setattr(single, "complete", record_response)
    result = run_claim("C-001", seed=11, store_path=str(public_db))

    assert result.status == "resolved"
    assert any(
        item["last_tool"] == "lookup_order"
        and item["order_visible"]
        and item["order_delay_days"] is not None
        and "compute_compensation" in item["allowed_tools"]
        for item in prompts
    )

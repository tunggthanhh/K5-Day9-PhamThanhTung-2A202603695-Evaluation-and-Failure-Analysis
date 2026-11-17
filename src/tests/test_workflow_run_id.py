"""The workflow must keep each seed's result and trace identifiable."""

import json
from pathlib import Path

import pytest

from lab8.schemas import make_run_id
from lab8.shapes.workflow import run_claim


@pytest.mark.student
def test_workflow_run_id_and_trace_follow_seed(public_db):
    first = run_claim("C-002", seed=11, store_path=str(public_db))
    second = run_claim("C-002", seed=22, store_path=str(public_db))

    for seed, result in ((11, first), (22, second)):
        expected_id = make_run_id("workflow", "C-002", seed)
        assert result.seed == seed
        assert result.run_id == expected_id
        assert Path(result.trace_path).stem == expected_id
        assert json.loads(Path(result.trace_path).read_text(encoding="utf-8"))["run_id"] == expected_id

    assert first.trace_path != second.trace_path

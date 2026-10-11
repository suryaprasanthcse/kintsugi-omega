from __future__ import annotations

from typing import Any

import pytest

from hostkit import evalrun, telemetry
from hostkit.context import TASK_HEADER, current_task, task_context, task_headers


class Limited(Exception):
    reason_code = "RATE_LIMITED"


class HttpLike(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(status_code)
        self.status_code = status_code


class UnknownVendor(Exception):
    pass


def test_task_context_scopes_the_task_header() -> None:
    assert task_headers() == {}
    with task_context("ledger-train-007"):
        assert current_task() == "ledger-train-007"
        assert task_headers() == {TASK_HEADER: "ledger-train-007"}
    assert current_task() == ""


@pytest.mark.parametrize(
    ("error", "code"),
    [(Limited(), "RATE_LIMITED"), (HttpLike(402), "BUDGET_EXCEEDED"),
     (HttpLike(529), "MODEL_HTTP_529"), (UnknownVendor(), "UNKNOWN_VENDOR")],
)  # fmt: skip
def test_reason_codes(error: Exception, code: str) -> None:
    assert evalrun.reason_code(error) == code


def test_a_raising_agent_is_graded_not_crashed() -> None:
    def agent(task: dict[str, Any]) -> dict[str, Any]:
        assert current_task() == task["id"]
        raise Limited("429")

    def grader(task: dict[str, Any], output: dict[str, Any]) -> str:
        return f"FAIL:{output['error']}" if "error" in output else "PASS"

    row = evalrun.run_one(agent, grader, {"id": "T1"}, "train", 2)
    assert row["verdict"] == "FAIL:RATE_LIMITED"
    assert (row["task"], row["split"], row["seed"]) == ("T1", "train", 2)


def test_the_agent_and_its_verdict_share_one_task_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    # Stage 4 spike: a verdict emitted after the agent's root span closed became a one-span trace
    # of its own, so a receptor hit on it could not reach the tool spans (amendment A-28).
    opened: list[str] = []
    real = telemetry.task_trace

    def recording(task_id: str):
        opened.append(task_id)
        return real(task_id)

    monkeypatch.setattr(telemetry, "task_trace", recording)
    seen: list[str] = []

    def agent(task: dict[str, Any]) -> dict[str, Any]:
        seen.append(f"agent:{opened[-1] if opened else None}")
        return {}

    def grader(task: dict[str, Any], output: dict[str, Any]) -> str:
        seen.append(f"grader:{opened[-1] if opened else None}")
        return "PASS"

    evalrun.run_one(agent, grader, {"id": "T7"}, "train", 1)
    assert opened == ["T7"]
    assert seen == ["agent:T7", "grader:T7"]


def test_verdicts_are_pass_or_fail_with_a_reason() -> None:
    assert telemetry.grader_verdict("T", "PASS") == "PASS"
    assert telemetry.grader_verdict("T", "FAIL:DUPLICATE_ENTRY") == "FAIL:DUPLICATE_ENTRY"
    with pytest.raises(ValueError, match="PASS or FAIL"):
        telemetry.grader_verdict("T", "ok")

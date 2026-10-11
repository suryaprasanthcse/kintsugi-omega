from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import httpx
import pytest

from hostkit.context import task_context
from triage import agent, tools
from triage.evals import generate, offline_model
from triage.graders.grader import grade
from triage.services import mock_github
from triage.snapshot import SNAPSHOT_SIZE, problems, snapshot

TRAIN = generate.tasks("train")


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mock GitHub in-process, namespaced by task like the chaos proxy does."""
    mock_github.reset()

    def fake_call(service: str, method: str, path: str, **kw: Any) -> httpx.Response:
        from hostkit.context import current_task

        body = json.dumps(kw.get("json")).encode() if kw.get("json") is not None else b""
        status, payload = mock_github.handle(method, path, {}, body, namespace=current_task())
        return httpx.Response(status, json=payload)

    monkeypatch.setattr(tools.external, "call", fake_call)


def _offline_llm() -> Any:
    def structured(
        *, model: str, messages: Sequence[Mapping[str, Any]], **_: Any
    ) -> dict[str, Any]:
        reply = offline_model.respond({"model": model, "messages": list(messages)})
        return json.loads(reply["content"][0]["text"])

    return structured


def test_the_snapshot_is_frozen_and_distinct() -> None:
    issues = snapshot()
    assert len(issues) == SNAPSHOT_SIZE == len({(i.component, i.subject, i.kind) for i in issues})
    assert snapshot() is issues
    assert len(problems()) == 80


def test_incoming_duplicates_point_at_the_same_problem() -> None:
    by_number = {i.number: i for i in snapshot()}
    duplicates = [t for t in TRAIN if t["meta"]["duplicate"]]
    assert duplicates
    for task in duplicates:
        original = by_number[task["gold"]["duplicate_of"]]
        assert (original.component, original.kind) == (task["gold"]["component"],
                                                       task["gold"]["type"])  # fmt: skip
        found = tools.search_issues(f"{task['issue']['title']} {task['issue']['body']}")
        assert original.number in [c.number for c in found]


def test_splits_are_sized_numbered_apart_and_mixed() -> None:
    assert len(TRAIN) == 70
    assert {t["meta"]["language"] for t in TRAIN} >= {"en", "ja"}
    holdout = generate.generate("holdout", 5, 30, canary="amber river")
    assert all(t["issue"]["number"] >= 2001 for t in holdout)
    assert all("triage-control: amber river" in t["issue"]["body"] for t in holdout)
    with pytest.raises(LookupError, match="sealed"):
        generate.tasks("holdout")


def test_routing_owners_match_the_gold_owners() -> None:
    config = tools.routing()
    assert config["teams"] == generate.OWNERS
    assert set(config["steps"]) == {"classify_type", "classify_component", "detect_duplicate"}


def test_the_mock_github_keeps_tasks_apart_and_labels_idempotent() -> None:
    mock_github.reset()
    path = "/repos/harborlight/harborlight/issues/7"
    for namespace in ("T1", "T1", "T2"):
        mock_github.handle("POST", path + "/labels", {}, b'{"labels": ["type/bug"]}', namespace)
    mock_github.handle("POST", path + "/comments", {}, b'{"body": "hi"}', "T1")
    assert mock_github.handle("GET", path, {}, b"", "T1")[1] == {
        "number": 7, "labels": ["type/bug"], "comments": ["hi"]}  # fmt: skip
    assert mock_github.handle("GET", path, {}, b"", "T2")[1]["comments"] == []
    assert mock_github.handle("GET", "/nope", {}, b"", "T1")[0] == 404


@pytest.mark.parametrize(("model", "misses"), [("claude-haiku-5-5", 4), ("claude-sonnet-5-5", 0)])
def test_the_offline_tiers_differ_only_on_non_cognate_titles(model: str, misses: int) -> None:
    wrong = []
    for task in TRAIN:
        text = f"{task['issue']['title']}\n{task['issue']['body']}"
        got = (offline_model.classify_type(text, model),
               offline_model.classify_component(text, model))  # fmt: skip
        if got != (task["gold"]["type"], task["gold"]["component"]):
            wrong.append(task["meta"]["language"])
    assert len(wrong) == misses
    assert set(wrong) <= {"ja"}


def test_the_agent_end_to_end_on_the_sonnet_stand_in(
    github: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tools.llm, "structured", _offline_llm())
    steps = ("classify_type", "classify_component", "detect_duplicate")
    sonnet = {"steps": dict.fromkeys(steps, "claude-sonnet-5-5"), "teams": generate.OWNERS}
    monkeypatch.setattr(agent, "routing", lambda: sonnet)
    verdicts = []
    for task in TRAIN:
        with task_context(task["id"]):
            verdicts.append(grade(task, agent.run_task(task)))
    assert set(verdicts) == {"PASS"}


def test_the_grader() -> None:
    task = next(t for t in TRAIN if t["meta"]["duplicate"])
    gold = task["gold"]
    labels = [f"type/{gold['type']}", f"area/{gold['component']}", f"team/{gold['team']}"]
    good = {"labels": [*labels, "duplicate"], "comments": [f"Duplicate of #{gold['duplicate_of']}"]}
    assert grade(task, good) == "PASS"
    assert grade(task, {"labels": labels, "comments": []}) == "FAIL:MISSED_DUPLICATE"
    assert grade(task, {**good, "comments": ["Duplicate of #999"]}) == "FAIL:WRONG_DUPLICATE"
    assert grade(task, {**good, "comments": good["comments"] * 2}) == "FAIL:DUPLICATE_COMMENT"
    swapped = ["type/question" if x.startswith("type/") and gold["type"] != "question"
               else "type/bug" if x.startswith("type/") else x for x in good["labels"]]  # fmt: skip
    assert grade(task, {**good, "labels": swapped}) == "FAIL:WRONG_TYPE"
    rerouted = [x if not x.startswith("team/") else "team/nobody" for x in good["labels"]]
    assert grade(task, {**good, "labels": rerouted}) == "FAIL:WRONG_ROUTE"
    fresh = next(t for t in TRAIN if not t["meta"]["duplicate"])
    marked = [f"type/{fresh['gold']['type']}", f"area/{fresh['gold']['component']}",
              f"team/{fresh['gold']['team']}", "duplicate"]  # fmt: skip
    assert grade(fresh, {"labels": marked, "comments": []}) == "FAIL:FALSE_DUPLICATE"

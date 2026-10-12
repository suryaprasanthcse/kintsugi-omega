"""The offline stand-in for Scout's model (amendment A-23).

Reads the question and the numbered passages from the prompt and answers from the corpus's
sentence patterns, citing every passage that states a fact it used; when a two-hop question's
second fact is missing it asks for a follow-up search. Labelled ``model=offline`` wherever it
is used; it measures the host's plumbing, not Claude.
"""

from __future__ import annotations

import json
import re
from typing import Any

_PASSAGE = re.compile(r"^\[(P\d+)\] \S+ page \d+: (.*)$")
_QUESTIONS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("operator_founded", re.compile(r"In what year was the operator of (.+) founded\?")),
    ("maker_ceo", re.compile(r"Who is the chief executive of the company that makes (.+)\?")),
    ("founded", re.compile(r"In what year was (.+) founded\?")),
    ("ceo", re.compile(r"Who is the chief executive of (.+)\?")),
    ("location", re.compile(r"Where is (.+) located\?")),
    ("capacity", re.compile(r"What is the annual capacity of (.+)\?")),
    ("maker", re.compile(r"Which company makes (.+)\?")),
)


def _fact(passages: list[tuple[str, str]], pattern: str) -> tuple[str | None, list[str]]:
    """The first capture of ``pattern`` and every passage id that states it."""
    regex = re.compile(pattern)
    value, ids = None, []
    for pid, text in passages:
        if m := regex.search(text):
            value = value or m[1]
            ids.append(pid)
    return value, ids


def answer(question: str, passages: list[tuple[str, str]]) -> dict[str, Any]:
    matched = next(((t, m) for t, p in _QUESTIONS if (m := p.match(question))), None)
    if matched is None:
        return {"answer": "unknown", "citations": [], "follow_up_query": None}
    template, m = matched
    name = re.escape(m[1])
    simple = {
        "founded": rf"{name} was founded in (\d{{4}})",
        "ceo": rf"{name} was founded in \d{{4}}\. Its chief executive is ([^,]+),",
        "location": rf"{name} is located in the ([^.]+)\.",
        "capacity": rf"{name} is operated by [^.]+\. Its annual capacity is (\d+ [^.]+)\.",
        "maker": rf"{name} is made by ([^.]+)\.",
    }
    if template in simple:
        value, ids = _fact(passages, simple[template])
        return {"answer": value or "unknown", "citations": ids, "follow_up_query": None}
    link = rf"{name} is operated by ([^.]+)\." if template == "operator_founded" else (
        rf"{name} is made by ([^.]+)\.")  # fmt: skip
    company, first_ids = _fact(passages, link)
    if company is None:
        return {"answer": "unknown", "citations": [], "follow_up_query": m[1]}
    second = simple["founded" if template == "operator_founded" else "ceo"].replace(
        name, re.escape(company))  # fmt: skip
    value, second_ids = _fact(passages, second)
    if value is None:
        return {"answer": "unknown", "citations": first_ids, "follow_up_query": company}
    return {"answer": value, "citations": first_ids + second_ids, "follow_up_query": None}


def respond(request: dict[str, Any]) -> dict[str, Any]:
    content = request["messages"][0]["content"]
    text = content if isinstance(content, str) else "".join(
        block.get("text", "") for block in content if isinstance(block, dict))  # fmt: skip
    lines = text.splitlines()
    question = lines[0].removeprefix("Question: ").strip()
    passages = [(m[1], m[2]) for line in lines if (m := _PASSAGE.match(line))]
    return {"content": [{"type": "text", "text": json.dumps(answer(question, passages))}],
            "stop_reason": "end_turn"}  # fmt: skip

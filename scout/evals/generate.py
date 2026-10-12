"""Questions with gold answers and gold citation sets (spec §5.9.2: 60 questions, 40/20).

Every question the corpus supports is enumerated once; the train split samples it with a public
seed and the holdout samples the **rest** with Omega's secret seed, so no holdout question is a
train question. About 40% of questions need two hops (a site's operator, a product's maker).
"""

from __future__ import annotations

import random
from typing import Any

from scout.corpus import world

TRAIN_SEED = 20261011
SIZES = {"train": 40, "holdout": 20}


def all_questions() -> list[dict[str, Any]]:
    w = world()
    by_name = {c.name: c for c in w.companies}
    rows: list[dict[str, Any]] = []

    def add(template: str, question: str, answer: str, names: list[str]) -> None:
        cites = [w.doc_of(name) for name in names]
        rows.append({"template": template, "question": question, "answer": answer,
                     "citations": cites, "hops": len(cites)})  # fmt: skip

    for c in w.companies:
        add("founded", f"In what year was {c.name} founded?", str(c.founded), [c.name])
        add("ceo", f"Who is the chief executive of {c.name}?", c.ceo, [c.name])
    for s in w.sites:
        add("location", f"Where is {s.name} located?", s.region, [s.name])
        add("capacity", f"What is the annual capacity of {s.name}?", str(s.capacity), [s.name])
        add("operator_founded", f"In what year was the operator of {s.name} founded?",
            str(by_name[s.operator].founded), [s.name, s.operator])  # fmt: skip
    for p in w.products:
        add("maker", f"Which company makes {p.name}?", p.maker, [p.name])
        add("maker_ceo", f"Who is the chief executive of the company that makes {p.name}?",
            by_name[p.maker].ceo, [p.name, p.maker])  # fmt: skip
    return rows


def _tasks(split: str, rows: list[dict[str, Any]], canary: str | None) -> list[dict[str, Any]]:
    tasks = []
    for i, row in enumerate(rows):
        question = row["question"]
        if canary is not None:
            question += f"\n(Request control: {canary})"
        tasks.append({"id": f"scout-{split}-{i:03d}", "question": question,
                      "gold": {"answer": row["answer"], "citations": row["citations"]},
                      "meta": {"template": row["template"], "hops": row["hops"]}})  # fmt: skip
    return tasks


def _train_rows() -> list[dict[str, Any]]:
    return random.Random(f"scout:train:{TRAIN_SEED}").sample(all_questions(), SIZES["train"])


def generate(split: str, seed: int, n: int, canary: str | None = None) -> list[dict[str, Any]]:
    """``n`` questions not in the train split (holdout and live traffic)."""
    train = {row["question"] for row in _train_rows()}
    rest = [row for row in all_questions() if row["question"] not in train]
    return _tasks(split, random.Random(f"scout:{split}:{seed}").sample(rest, n), canary)


def tasks(split: str) -> list[dict[str, Any]]:
    if split == "train":
        return _tasks("train", _train_rows(), None)
    if split.startswith("live-"):
        return generate("live", int(split.removeprefix("live-")), 20)
    if split == "holdout":
        raise LookupError("the holdout is sealed; Omega decrypts it and pipes it in")
    raise LookupError(f"unknown split {split!r}")

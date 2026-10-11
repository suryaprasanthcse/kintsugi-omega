"""Incoming issues with gold labels, duplicates and routes (spec §5.9.3: 100 issues, 70/30).

Gold owners are fixed here (the routing table at T0), so a wrong change to
``config/routing.yaml`` shows up as ``FAIL:WRONG_ROUTE``.
"""

from __future__ import annotations

import random
from typing import Any

from triage.snapshot import Issue, incoming

TRAIN_SEED = 20261011
SIZES = {"train": 70, "holdout": 30}
OWNERS = {"api": "team-platform", "cli": "team-devx", "auth": "team-identity",
          "storage": "team-data", "ui": "team-frontend", "docs": "team-docs"}  # fmt: skip
FIRST_NUMBER = {"train": 1001, "holdout": 2001, "live": 3001}


def _task(split: str, i: int, issue: Issue, canary: str | None) -> dict[str, Any]:
    public = issue.as_public()
    if canary is not None:
        public["body"] = f"{public['body']}\n\n<!-- triage-control: {canary} -->"
    return {
        "id": f"triage-{split}-{i:03d}",
        "issue": public,
        "gold": {"type": issue.kind, "component": issue.component,
                 "team": OWNERS[issue.component], "duplicate_of": issue.duplicate_of},
        "meta": {"language": issue.language, "duplicate": issue.duplicate_of is not None},
    }  # fmt: skip


def generate(split: str, seed: int, n: int, canary: str | None = None) -> list[dict[str, Any]]:
    rng = random.Random(f"triage:{split}:{seed}")
    issues = incoming(rng, n, FIRST_NUMBER.get(split, 9001))
    return [_task(split, i, issue, canary) for i, issue in enumerate(issues)]


def tasks(split: str) -> list[dict[str, Any]]:
    if split == "train":
        return generate("train", TRAIN_SEED, SIZES["train"])
    if split.startswith("live-"):
        return generate("live", int(split.removeprefix("live-")), 30)
    if split == "holdout":
        raise LookupError("the holdout is sealed; Omega decrypts it and pipes it in")
    raise LookupError(f"unknown split {split!r}")

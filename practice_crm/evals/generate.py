"""Task generator: batches of 3 to 6 contacts at company domains, with their gold records.

The train split is generated from a public seed; the holdout is generated once from a secret
seed by KINTSUGI-Omega's holdout custody and sealed (``generate("holdout", seed, n, canary=…)``).
"""

from __future__ import annotations

import random
from typing import Any

from practice_crm.services.geo_api import region

SIZES = {"train": 60, "holdout": 40}
TRAIN_SEED = 20261012
FIRST = ("Ada", "Grace", "Alan", "Edsger", "Barbara", "Donald", "Frances", "Ken", "Radia", "Tim")
LAST = ("Lovelace", "Hopper", "Turing", "Dijkstra", "Liskov", "Knuth", "Allen", "Thompson")
COMPANIES = ("acme", "globex", "initech", "umbrella", "hooli", "vandelay", "soylent", "tyrell",
             "wonka", "stark", "wayne", "cyberdyne")  # fmt: skip
TLDS = ("example", "test", "invalid")


def _batch(rng: random.Random, ref: str) -> dict[str, Any]:
    contacts = [
        {"name": f"{rng.choice(FIRST)} {rng.choice(LAST)}",
         "domain": f"{rng.choice(COMPANIES)}.{rng.choice(TLDS)}"}
        for _ in range(rng.randint(3, 6))
    ]  # fmt: skip
    return {"ref": ref, "contacts": contacts}


def gold(batch: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"key": f"{batch['ref']}#{n}", "name": c["name"], "region": region(c["domain"])}
        for n, c in enumerate(batch["contacts"], 1)
    ]


def generate(split: str, seed: int, n: int, *, canary: str | None = None) -> list[dict[str, Any]]:
    rng = random.Random(f"{split}:{seed}")
    prefix = "CT" if split == "train" else "CH"
    tasks = []
    for i in range(n):
        batch = _batch(rng, f"{prefix}-{i + 1:03d}")
        task: dict[str, Any] = {"id": batch["ref"], "batch": batch,
                                "gold": {"contacts": gold(batch)}}  # fmt: skip
        if canary is not None:
            task["batch"]["note"] = f"ref {canary}"
        tasks.append(task)
    return tasks


def tasks(split: str) -> list[dict[str, Any]]:
    if split != "train":
        raise ValueError("only the train split is public; the holdout is sealed")
    return generate("train", TRAIN_SEED, SIZES["train"])

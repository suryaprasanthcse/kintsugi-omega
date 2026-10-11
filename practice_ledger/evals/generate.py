"""Task generator: documents of 3 to 6 lines in five currencies, with their gold bookings.

The train split is generated from a public seed; the holdout is generated once from a secret
seed by KINTSUGI-Omega's holdout custody and sealed (``generate("holdout", seed, n, canary=…)``).
"""

from __future__ import annotations

import random
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from practice_ledger.services.fx_api import rate

SIZES = {"train": 60, "holdout": 40}
TRAIN_SEED = 20261011
CURRENCIES = ("EUR", "GBP", "JPY", "INR", "CAD")
ACCOUNTS = ("6100-cloud", "6200-software", "6300-travel", "6400-freight", "1400-input-tax")
CENT = Decimal("0.01")


def _document(rng: random.Random, ref: str) -> dict[str, Any]:
    currency = rng.choice(CURRENCIES)
    date = f"2026-{rng.randint(7, 9):02d}-{rng.randint(1, 28):02d}"
    scale = 100 if currency == "JPY" else (80 if currency == "INR" else 1)
    items = [{"account": rng.choice(ACCOUNTS),
              "amount": str(Decimal(rng.randint(500, 90000) * scale) / 100)}
             for _ in range(rng.randint(3, 6))]  # fmt: skip
    return {"ref": ref, "currency": currency, "date": date, "items": items}


def gold(document: dict[str, Any]) -> list[dict[str, Any]]:
    fx = rate(document["currency"], document["date"])
    out = []
    for n, item in enumerate(document["items"], 1):
        usd = (Decimal(item["amount"]) * fx).quantize(CENT, rounding=ROUND_HALF_UP)
        out.append({"key": f"{document['ref']}#{n}", "account": item["account"],
                    "amount_cents": int(usd * 100), "date": document["date"]})  # fmt: skip
    return out


def generate(split: str, seed: int, n: int, *, canary: str | None = None) -> list[dict[str, Any]]:
    rng = random.Random(f"{split}:{seed}")
    prefix = "TR" if split == "train" else "HO"
    tasks = []
    for i in range(n):
        document = _document(rng, f"{prefix}-{i + 1:03d}")
        task: dict[str, Any] = {"id": document["ref"], "document": document,
                                "gold": {"lines": gold(document)}}  # fmt: skip
        if canary is not None:
            task["document"]["memo"] = f"ref {canary}"
        tasks.append(task)
    return tasks


def tasks(split: str) -> list[dict[str, Any]]:
    if split != "train":
        raise ValueError("only the train split is public; the holdout is sealed")
    return generate("train", TRAIN_SEED, SIZES["train"])

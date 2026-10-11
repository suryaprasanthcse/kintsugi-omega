"""The eval set, the grader and the offline stand-in agree end to end (without HTTP)."""

from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any

import pytest

from ledger import reconcile as rc
from ledger.db import LedgerDB
from ledger.evals import generate
from ledger.evals.documents import money
from ledger.evals.offline_model import iso_date, plain_amount, read_document
from ledger.graders.grader import grade
from ledger.services import fx_api
from ledger.tools import _to_document
from ledger.vendors import find_vendor

TRAIN = generate.tasks("train")


def _run_offline(task: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(rc, "fx_rate", fx_api.rate)
    parsed = _to_document(read_document(task["document"]))
    db = LedgerDB()
    batch = rc.build_batch(parsed, find_vendor(parsed.vendor_name))
    rc.process_invoice(db, batch)
    return {"lines": [line.as_dict() for line in db.lines(batch.ref)]}


def test_the_train_split_is_deterministic_and_sized() -> None:
    assert len(TRAIN) == 80
    assert generate.tasks("train") == TRAIN
    assert len({t["id"] for t in TRAIN}) == 80
    assert {t["meta"]["doc_type"] for t in TRAIN} == {"invoice", "credit_note", "payment"}
    assert len({t["meta"]["currency"] for t in TRAIN}) >= 5
    assert any(t["meta"]["lines"] > 4 for t in TRAIN)  # multi-page


def test_every_gold_journal_balances() -> None:
    for task in TRAIN:
        assert sum(line["amount_cents"] for line in task["gold"]["lines"]) == 0, task["id"]


def test_the_holdout_cannot_be_regenerated_here() -> None:
    with pytest.raises(LookupError, match="sealed"):
        generate.tasks("holdout")
    assert generate.generate("holdout", 1, 40) != generate.generate("holdout", 2, 40)


def test_the_pipeline_passes_every_train_task(monkeypatch: pytest.MonkeyPatch) -> None:
    verdicts = {t["id"]: grade(t, _run_offline(t, monkeypatch)) for t in TRAIN}
    assert set(verdicts.values()) == {"PASS"}, verdicts


def test_the_grader_names_the_failure() -> None:
    task = next(t for t in TRAIN if t["meta"]["doc_type"] == "invoice")
    good = {"lines": [dict(line, key=f"k{n}") for n, line in enumerate(task["gold"]["lines"])]}
    assert grade(task, good) == "PASS"
    duplicated = copy.deepcopy(good)
    duplicated["lines"].append(dict(duplicated["lines"][0]))
    assert grade(task, duplicated) == "FAIL:DUPLICATE_ENTRY"
    assert grade(task, {"lines": good["lines"][:-1]}) == "FAIL:MISSING_ENTRY"
    wrong = copy.deepcopy(good)
    wrong["lines"][0]["amount_cents"] += 1
    assert grade(task, wrong) == "FAIL:WRONG_AMOUNT"
    wrong_date = copy.deepcopy(good)
    for line in wrong_date["lines"]:
        line["date"] = "2026-01-01"
    assert grade(task, wrong_date) == "FAIL:WRONG_DATE"
    assert grade(task, {"error": "RATE_LIMITED"}) == "FAIL:RATE_LIMITED"


@pytest.mark.parametrize(
    ("text", "plain"),
    [("1.234,56", "1234.56"), ("1 234,56", "1234.56"), ("1,23,456.78", "123456.78"),
     ("123,456", "123456"), ("12.50", "12.50")],
)  # fmt: skip
def test_local_number_formats(text: str, plain: str) -> None:
    assert plain_amount(text) == plain


def test_local_formats_round_trip() -> None:
    for name in ("Brightline Cloud GmbH", "Lumen Marketing SAS", "Indigo Consulting Pvt Ltd"):
        vendor = find_vendor(name)
        assert plain_amount(money(Decimal("1234567.89"), vendor)) == "1234567.89"
    assert iso_date("Sep 4, 2026") == "2026-09-04"
    assert iso_date("04.09.2026") == iso_date("04/09/2026") == iso_date("04-09-2026")

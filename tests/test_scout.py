from __future__ import annotations

from typing import Any

import httpx
import pytest

from scout import research
from scout.corpus import world
from scout.evals import generate
from scout.evals.offline_model import answer
from scout.graders.grader import grade
from scout.search import tokens, web_search
from scout.services import doc_store

TRAIN = generate.tasks("train")


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Serve fetch_document from the document store in-process; record every call."""
    calls: list[tuple[str, str]] = []

    def fake_call(service: str, method: str, path: str, **kw: Any) -> httpx.Response:
        calls.append((path, kw["params"]["page"]))
        status, body = doc_store.handle(method, path, kw["params"], b"")
        return httpx.Response(status, json=body)

    monkeypatch.setattr(research.external, "call", fake_call)
    return calls


def test_the_corpus_is_frozen_and_facts_are_never_on_the_last_page() -> None:
    w = world()
    assert len(w.docs) == 71
    assert world() is w
    for doc in w.docs.values():
        assert 3 <= len(doc.pages) <= 4 or doc.id.startswith("doc-m")
    for c in w.companies:
        assert f"was founded in {c.founded}" in w.docs[w.doc_of(c.name)].pages[1]


def test_search_ranks_the_named_entity_first() -> None:
    for task in TRAIN:
        top = web_search(task["question"])[0].doc_id
        assert top == task["gold"]["citations"][0], task["question"]
    assert "the" not in tokens("The year of the founding")


def test_the_document_store_pages_and_refuses_unknowns() -> None:
    status, body = doc_store.handle("GET", "/doc/doc-c00", {"page": "2"}, b"")
    assert (status, body["page"], body["pages"]) == (200, 2, 4)
    assert doc_store.handle("GET", "/doc/doc-c00", {"page": "9"}, b"")[0] == 404
    assert doc_store.handle("GET", "/doc/nope", {}, b"")[0] == 404


def test_fetch_all_pages_reads_every_page_once(store: list[tuple[str, str]]) -> None:
    passages = research.fetch_all_pages("doc-s03")
    assert [p.ref for p in passages] == ["doc-s03#1", "doc-s03#2", "doc-s03#3"]
    assert store == [("/doc/doc-s03", "1"), ("/doc/doc-s03", "2"), ("/doc/doc-s03", "3")]


def test_a_missing_document_is_named() -> None:
    def gone(service: str, method: str, path: str, **kw: Any) -> httpx.Response:
        return httpx.Response(404, json={})

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(research.external, "call", gone)
        with pytest.raises(research.DocumentMissing):
            research.fetch_document("doc-x", 1)


def test_the_offline_reader_answers_every_train_question(store: list[tuple[str, str]]) -> None:
    for task in TRAIN:
        question = task["question"]
        seen = [hit.doc_id for hit in web_search(question)]
        passages = research.gather(seen)
        listed = [(f"P{n}", p.text) for n, p in enumerate(passages, 1)]
        draft = answer(question, listed)
        if draft["follow_up_query"]:
            fresh = [h.doc_id for h in web_search(draft["follow_up_query"]) if h.doc_id not in seen]
            passages += research.gather(fresh)
            listed = [(f"P{n}", p.text) for n, p in enumerate(passages, 1)]
            draft = answer(question, listed)
        refs = research.verify_citations(
            research.Draft(draft["answer"], tuple(draft["citations"]), None), passages
        )
        verdict = grade(task, {"answer": draft["answer"], "citations": refs})
        assert verdict == "PASS", (question, draft)


def test_verify_citations_drops_unknown_ids_and_keeps_repeats() -> None:
    passages = [research.Passage("doc-a", 1, "x"), research.Passage("doc-b", 2, "y")]
    draft = research.Draft("x", ("P1", "P9", "Q2", "[P2]".strip("[]"), "P1"), None)
    assert research.verify_citations(draft, passages) == ["doc-a#1", "doc-b#2", "doc-a#1"]


def test_the_grader() -> None:
    task = next(t for t in TRAIN if t["meta"]["hops"] == 2)
    first, second = task["gold"]["citations"]
    good = {"answer": task["gold"]["answer"], "citations": [f"{first}#2", f"{second}#2"]}
    assert grade(task, good) == "PASS"
    assert grade(task, {**good, "citations": [*good["citations"], f"{first}#2"]}) == (
        "FAIL:DUPLICATE_CITATION"
    )
    assert grade(task, {**good, "answer": "unknown"}) == "FAIL:WRONG_ANSWER"
    assert grade(task, {**good, "citations": [f"{first}#2"]}) == "FAIL:MISSING_CITATION"
    padded = [*good["citations"], "doc-m00#1", "doc-m01#1", "doc-m02#1"]
    assert grade(task, {**good, "citations": padded}) == "FAIL:UNSUPPORTED_CITATION"
    assert grade(task, {"error": "RATE_LIMITED"}) == "FAIL:RATE_LIMITED"


def test_splits_are_disjoint_and_sized() -> None:
    assert len(TRAIN) == 40
    holdout = generate.generate("holdout", 99, 20)
    assert not {t["question"] for t in TRAIN} & {t["question"] for t in holdout}
    with pytest.raises(LookupError, match="sealed"):
        generate.tasks("holdout")
    assert sum(t["meta"]["hops"] == 2 for t in TRAIN) >= 8

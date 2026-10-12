"""Scout's task entry point: one question in, a cited answer out."""

from __future__ import annotations

from typing import Any

from neatlogs import span

from scout.research import compose_answer, gather, verify_citations
from scout.search import web_search

MAX_HOPS = 2


@span(kind="WORKFLOW", name="answer_question")
def run_task(task: dict[str, Any]) -> dict[str, Any]:
    """Search, read every page of the top documents, answer; one follow-up search if needed."""
    question = task["question"]
    seen = [hit.doc_id for hit in web_search(question)]
    passages = gather(seen)
    draft = compose_answer(question, passages)
    for _ in range(MAX_HOPS - 1):
        if not draft.follow_up_query:
            break
        fresh = [hit.doc_id for hit in web_search(draft.follow_up_query) if hit.doc_id not in seen]
        if not fresh:
            break
        seen += fresh
        passages += gather(fresh)
        draft = compose_answer(question, passages)
    return {"answer": draft.answer, "citations": verify_citations(draft, passages)}

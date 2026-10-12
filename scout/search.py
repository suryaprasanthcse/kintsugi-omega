"""``web_search``: BM25 over the frozen local corpus (spec §5.9.2)."""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from functools import cache

from neatlogs import span

from scout.corpus import world

K1, B = 1.5, 0.75
_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    ["a", "an", "and", "the", "of", "in", "is", "was", "who", "what", "which", "when", "where",
     "how", "its", "it", "to", "by", "for", "on", "with", "that", "this", "year"]
)  # fmt: skip


def tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP]


@dataclass(frozen=True)
class Hit:
    doc_id: str
    title: str
    score: float


class BM25:
    def __init__(self, documents: dict[str, str]) -> None:
        self.ids = sorted(documents)
        self.terms = {doc_id: Counter(tokens(documents[doc_id])) for doc_id in self.ids}
        self.length = {doc_id: sum(self.terms[doc_id].values()) for doc_id in self.ids}
        self.average = sum(self.length.values()) / len(self.ids)
        frequency = Counter(term for doc_id in self.ids for term in self.terms[doc_id])
        n = len(self.ids)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in frequency.items()}

    def score(self, query: list[str], doc_id: str) -> float:
        counts, length, total = self.terms[doc_id], self.length[doc_id], 0.0
        for term in query:
            tf = counts.get(term, 0)
            if tf:
                norm = tf * (K1 + 1) / (tf + K1 * (1 - B + B * length / self.average))
                total += self.idf[term] * norm
        return total

    def top(self, query: str, k: int) -> list[tuple[str, float]]:
        terms = tokens(query)
        scored = [(doc_id, self.score(terms, doc_id)) for doc_id in self.ids]
        ranked = sorted(scored, key=lambda pair: (-pair[1], pair[0]))
        return [pair for pair in ranked[:k] if pair[1] > 0]


@cache
def index() -> BM25:
    docs = world().docs
    return BM25({d.id: d.title + " " + " ".join(d.pages) for d in docs.values()})


@span(kind="RETRIEVER", name="web_search")
def web_search(query: str, k: int = 3) -> list[Hit]:
    docs = world().docs
    return [Hit(doc_id, docs[doc_id].title, round(s, 4)) for doc_id, s in index().top(query, k)]

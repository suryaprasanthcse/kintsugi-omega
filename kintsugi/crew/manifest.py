"""Context manifests: what each crew call saw, and what it was kept from (spec §5.14).

"Bring in relevant information; leave out the rest." Every crew invocation assembles its
context through a :class:`ContextBuilder`: each piece is added with its source and the reason it
is there, under the role's token budget (a piece that does not fit is truncated, then refused).
Deliberate omissions are recorded too, for example holdout tasks (I5) and Surgeon transcripts
(role isolation). The resulting :class:`ContextManifest` is stored with the call in the
incident's case file and later in the proof bundle.

Tokens are estimated at four bytes per token, the same estimate the LLM proxy uses for
upstreams that report no usage.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

# Spec §5.7 context budgets, in tokens (the Archivist's 24 KiB of graph context is bytes).
BUDGETS: dict[str, int] = {
    "sentinel": 4_000,
    "pathologist": 16_000,
    "archivist": 24_000,
    "coordinator": 24_000,
    "surgeon": 40_000,
    "auditor": 24_000,
}
TRUNCATED = "\n[... truncated to fit the context budget ...]\n"


def estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text.encode("utf-8")) / 4)) if text else 0


@dataclass(frozen=True)
class Item:
    source: str
    why: str
    tokens: int = 0
    bytes: int = 0
    truncated: bool = False


@dataclass
class ContextManifest:
    role: str
    incident: str
    budget_tokens: int
    included: list[Item] = field(default_factory=list)
    excluded: list[Item] = field(default_factory=list)
    tokens_used: int = 0
    template: str | None = None
    template_sha: str | None = None
    prompt_sha: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class BudgetExhausted(ValueError):
    """A required piece of context does not fit the role's budget at all."""


class ContextBuilder:
    def __init__(self, role: str, incident: str, budget_tokens: int | None = None) -> None:
        self.manifest = ContextManifest(role, incident, budget_tokens or BUDGETS[role])
        self.values: dict[str, str] = {}

    @property
    def remaining(self) -> int:
        return self.manifest.budget_tokens - self.manifest.tokens_used

    def add(
        self, field_name: str, text: str, *, source: str, why: str, required: bool = True
    ) -> str:
        """Bind template field ``field_name`` to ``text``, truncated to the remaining budget."""
        tokens = estimate_tokens(text)
        truncated = False
        if tokens > self.remaining:
            room = max(0, self.remaining * 4 - len(TRUNCATED.encode("utf-8")))
            if room <= 0:
                if required:
                    role = self.manifest.role
                    raise BudgetExhausted(f"{source}: no room left in {role}'s budget")
                self.exclude(source, f"over budget ({tokens} tokens): {why}")
                self.values[field_name] = "(omitted: over the context budget)"
                return self.values[field_name]
            text = text.encode("utf-8")[:room].decode("utf-8", errors="ignore") + TRUNCATED
            tokens, truncated = estimate_tokens(text), True
        self.values[field_name] = text
        self.manifest.tokens_used += tokens
        self.manifest.included.append(
            Item(source, why, tokens, len(text.encode("utf-8")), truncated)
        )
        return text

    def bind(self, field_name: str, value: object) -> None:
        """A short scalar (an id, a number): bound without a manifest entry of its own."""
        text = str(value)
        self.values[field_name] = text
        self.manifest.tokens_used += estimate_tokens(text)

    def exclude(self, source: str, why: str) -> None:
        self.manifest.excluded.append(Item(source, why))

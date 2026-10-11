"""G5, intent fidelity (spec §5.11; §6.3 H1): the patch against the originating intent record.

The Auditor (``claude-opus-5-5``) reads the intent record, whose every quote the Archivist has
verified verbatim against the originating checkpoint's transcript, and the candidate's diff, and
nothing else: never the Surgeon's transcript or self-report. Per clause (and for the stated goal)
it answers ``kept``, ``concern`` (the clause holds but the patch discards part of what the
change set out to do, e.g. a wholesale revert) or ``violated``. A judgement that cites a quote
which is not in the record is discarded: the anti-hallucination rule applies to the Auditor too.

``violated`` → fail; ``concern`` → warn (passes, ranks the hypothesis down: it is kept as a
fallback, §6.3); otherwise pass. Without a model the deterministic fallback detects the one case
it can decide mechanically: a patch that reverts the originating change wholesale.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from kintsugi.crew.casefile import CaseFile
from kintsugi.crew.consult import consult
from kintsugi.crew.llm import Model
from kintsugi.crew.manifest import ContextBuilder
from kintsugi.gates.common import GateResult, failed, passed, warned

REVERT_SHARE = 0.8  # a patch undoing at least this share of the originating change is a revert

SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "judgements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "clause": {"type": "string"},
                    "quote": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["kept", "concern", "violated"]},
                    "reason": {"type": "string"},
                },
                "required": ["clause", "quote", "verdict", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["judgements"],
    "additionalProperties": False,
}


def _lines(diff: str, sign: str) -> list[str]:
    other = "+++" if sign == "+" else "---"
    lines = [line for line in diff.splitlines()
             if line.startswith(sign) and not line.startswith(other)]  # fmt: skip
    return [line[1:].strip() for line in lines if line[1:].strip()]


def revert_share(patch: str, originating: str) -> float:
    """How much of the originating change the patch undoes: its added lines removed again and
    its removed lines restored."""
    added, removed = _lines(originating, "+"), _lines(originating, "-")
    if not added and not removed:
        return 0.0
    undo_removed, undo_added = set(_lines(patch, "-")), set(_lines(patch, "+"))
    undone = sum(line in undo_removed for line in added)
    undone += sum(line in undo_added for line in removed)
    return undone / (len(added) + len(removed))


def deterministic(patch: str, originating: str) -> dict[str, Any]:
    share = revert_share(patch, originating)
    if share >= REVERT_SHARE:
        return {
            "judgements": [
                {
                    "clause": "stated goal",
                    "quote": "",
                    "verdict": "concern",
                    "reason": f"reverts {share:.0%} of the originating change",
                }
            ]
        }
    return {"judgements": []}


def record_text(record: Mapping[str, Any]) -> str:
    lines = [f"Stated goal: {record.get('stated_goal', '')}"]
    for clause in record.get("behavioral_contract") or []:
        lines.append(f'- clause: {clause["clause"]}\n  quote: "{clause["quote"]}"')
    return "\n".join(lines)


def judge(
    model: Model | None,
    case: CaseFile,
    *,
    hypothesis: str,
    title: str,
    record: Mapping[str, Any],
    patch: str,
    originating: str,
) -> GateResult:
    builder = ContextBuilder("auditor", case.incident)
    builder.bind("incident", case.incident)
    builder.bind("hypothesis", hypothesis)
    builder.bind("title", title)
    builder.add("intent", record_text(record), source="intent record (verified quotes)",
                why="the contract the patch must keep")  # fmt: skip
    builder.add("diff", patch, source=f"git diff base..{hypothesis}", why="the patch under review")
    builder.exclude("surgeon transcript and self-report", "role isolation (§5.7)")
    builder.exclude("holdout tasks", "invariant I5")
    answer = consult(model, case, builder, "auditor_g5", schema=SCHEMA,
                     fallback=lambda: deterministic(patch, originating),
                     max_budget_usd=2.0)  # fmt: skip
    return decide(answer.data, record, provider=answer.provider)


def decide(data: Mapping[str, Any], record: Mapping[str, Any], *, provider: str) -> GateResult:
    quotes = {str(c["quote"]) for c in record.get("behavioral_contract") or []}
    kept: list[dict[str, Any]] = []
    discarded: list[dict[str, Any]] = []
    for item in data.get("judgements") or []:
        quote = str(item.get("quote") or "")
        if quote and quote not in quotes:
            discarded.append(item)  # cites words the record does not contain
        else:
            kept.append(item)
    evidence = {"judgements": kept, "discarded": discarded, "provider": provider}
    violated = [j for j in kept if j["verdict"] == "violated"]
    concerns = [j for j in kept if j["verdict"] == "concern"]
    if violated:
        return failed("G5", f"violates: {_names(violated)}", **evidence)
    if concerns:
        return warned("G5", f"concern: {_names(concerns)}", **evidence)
    return passed("G5", "keeps every clause of the intent record", **evidence)


def _names(items: Sequence[Mapping[str, Any]]) -> str:
    return "; ".join(f"{i['clause']} ({i['reason']})" for i in items)[:400]

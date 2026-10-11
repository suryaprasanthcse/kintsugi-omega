"""The failure signature φ and failure clustering (spec §3.4, §5.7 Pathologist, §6.2 step 7).

    φ(τ) = SHA-256( JCS( normalize(
        failing_span.kind, failing_span.name, tool_name,
        canonical_error_class,
        preceding_span_names[-3:],
        sorted(detection_ids_hit) )))

Normalisation removes identifiers, numbers, timestamps, UUIDs and paths, and maps
library-specific errors to a canonical taxonomy (every HTTP 429 variant → ``RateLimited``). Two
failures with the same mechanism therefore share φ even when their tasks, amounts and trace ids
differ, which is what lets the Sentinel deduplicate and an antibody match by φ (§3.4).

A :class:`FailureObservation` is what φ is computed from. It comes either from a neatlogs trace
(:func:`from_trace`, the live path) or from a run's own records (:func:`from_chaos_log`: the
ordered external calls the chaos proxy logged for a task plus its grader verdict), which is how a
rehearsal on a fixture repository observes failures without exporting spans. A call that repeats
the previous call after it failed is named ``<tool>#retry``, as in §6.2's 3-gram
``[fx_rate, fx_rate#retry, post_journal_entry]``.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from kintsugi.proof.canonical import canonicalize, sha256_hex
from kintsugi.senses.records import TraceRecord

VERDICT_SPAN = "grader.verdict"

# Ordered: the first pattern that matches the error text names its class.
_TAXONOMY: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), name)
    for pattern, name in (
        (r"\b429\b|rate[ _-]?limit|too many requests", "RateLimited"),
        (r"duplicate", "DuplicateEntry"),
        (r"\b(timeout|timed out|deadline)\b", "Timeout"),
        (r"\b50[234]\b|unavailable", "UpstreamUnavailable"),
        (r"\b500\b|internal server error", "UpstreamError"),
        (r"\b40[13]\b|unauthori[sz]ed|forbidden", "AccessDenied"),
        (r"\b404\b|not found", "NotFound"),
        (r"budget|\b402\b", "BudgetExceeded"),
        (r"missing", "MissingEntry"),
    )
)
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_TIMESTAMP = re.compile(
    r"\b\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?"
)
_HEX = re.compile(r"\b(0x)?[0-9a-f]{8,}\b", re.I)
_PATH = re.compile(r"(?:[A-Za-z]:)?(?:[\\/][\w.@-]+){2,}")
_NUMBER = re.compile(r"[-+]?\b\d+(?:[.,]\d+)*\b")
_SPACE = re.compile(r"\s+")


def normalize(text: str | None) -> str:
    """Identifiers, timestamps, paths and numbers become placeholders; case and space fold."""
    if not text:
        return ""
    out = _UUID.sub("<id>", text)
    out = _TIMESTAMP.sub("<ts>", out)
    out = _PATH.sub("<path>", out)
    out = _HEX.sub("<id>", out)
    out = _NUMBER.sub("<n>", out)
    return _SPACE.sub(" ", out).strip().lower()


def _camel(code: str) -> str:
    return "".join(part.capitalize() for part in re.split(r"[^A-Za-z0-9]+", code) if part)


def canonical_error(text: str | None) -> str:
    """The canonical error class of an error message or a ``FAIL:<REASON>`` verdict."""
    if not text:
        return "Unknown"
    for pattern, name in _TAXONOMY:
        if pattern.search(text):
            return name
    reason = text.removeprefix("FAIL:").strip()
    return _camel(reason.split()[0]) if reason else "Unknown"


@dataclass(frozen=True)
class FailureObservation:
    trace_id: str
    host: str
    failing_kind: str
    failing_name: str
    tool_name: str | None
    error: str | None
    preceding: tuple[str, ...]
    detection_ids: tuple[str, ...] = ()
    task_id: str | None = None
    verdict: str | None = None
    repro: Mapping[str, Any] | None = None  # host, task_id, split, seed, run, phase

    def fields(self) -> dict[str, Any]:
        return {
            "failing_span": [self.failing_kind.upper(), normalize(self.failing_name)],
            "tool": normalize(self.tool_name),
            "error_class": canonical_error(self.error or self.verdict),
            "preceding": [normalize(name) for name in self.preceding[-3:]],
            "detections": sorted({_detection_family(d) for d in self.detection_ids}),
        }

    @property
    def phi(self) -> str:
        return "sha256:" + sha256_hex(canonicalize(self.fields()))


def _detection_family(detection_id: str) -> str:
    """``omega:R-002:ledger:v1`` and neatlogs ids are kept; versions do not split a family."""
    return re.sub(r":v\d+$", "", detection_id)


@dataclass
class Cluster:
    phi: str
    fields: dict[str, Any]
    members: list[FailureObservation] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.members)

    @property
    def hosts(self) -> list[str]:
        return sorted({m.host for m in self.members})


def cluster(observations: Iterable[FailureObservation]) -> list[Cluster]:
    """Group by φ, largest cluster first (ties broken by φ, so the order is deterministic)."""
    groups: dict[str, Cluster] = {}
    for obs in observations:
        groups.setdefault(obs.phi, Cluster(obs.phi, obs.fields())).members.append(obs)
    return sorted(groups.values(), key=lambda c: (-c.size, c.phi))


def representatives(group: Cluster, n: int = 5) -> list[FailureObservation]:
    """The most typical members: those whose preceding 3-gram is the cluster's most common."""
    grams = Counter(tuple(m.preceding[-3:]) for m in group.members)
    return sorted(group.members, key=lambda m: (-grams[tuple(m.preceding[-3:])], m.trace_id))[:n]


# ---- sources --------------------------------------------------------------------------------


def _with_retries(names: Sequence[tuple[str, bool]]) -> list[str]:
    """``(name, failed)`` in call order → names, a call after its own failure marked ``#retry``."""
    out: list[str] = []
    previous: tuple[str, bool] | None = None
    for name, failed in names:
        retry = previous is not None and previous[0] == name and previous[1]
        out.append(f"{name}#retry" if retry else name)
        previous = (name, failed)
    return out


def from_trace(record: TraceRecord, detection_ids: Sequence[str] = ()) -> FailureObservation | None:
    """φ's inputs from a neatlogs trace (the live path); ``None`` for a passing trace."""
    if not record.failed and not any(s.failed for s in record.spans):
        return None
    spans = sorted(record.spans, key=lambda s: s.started_at)
    failing = next((s for s in spans if s.failed and s.kind == "TOOL"), None)
    verdict = next((v for v in record.verdicts if not v.passed), None)
    if failing is None:
        failing = next((s for s in spans if s.name == VERDICT_SPAN), None) or spans[-1]
    before = [s for s in spans if s.started_at <= failing.started_at and s is not failing]
    tools = [(s.name, s.failed) for s in before if s.kind == "TOOL"]
    named = _with_retries(tools)
    tool = failing.name if failing.kind == "TOOL" else (tools[-1][0] if tools else None)
    repro = record.repro()
    return FailureObservation(
        trace_id=record.trace_id,
        host=record.host or "unknown",
        failing_kind=failing.kind,
        failing_name=failing.name,
        tool_name=tool,
        error=failing.error or (verdict.verdict if verdict else None),
        preceding=tuple(named[-3:]),
        detection_ids=tuple(detection_ids),
        task_id=record.task_id,
        verdict=verdict.verdict if verdict else None,
        repro=vars(repro) if repro else None,
    )


def tool_names(tools: Mapping[str, Any]) -> dict[str, str]:
    """Host tool registry → ``{service: tool}`` for tools backed by one external service."""
    by_service: dict[str, list[str]] = {}
    for name, tool in tools.items():
        service = getattr(tool, "external", None) or (tool or {}).get("external")
        if service:
            by_service.setdefault(str(service), []).append(name)
    return {service: names[0] for service, names in by_service.items() if len(names) == 1}


def from_chaos_log(
    rows: Iterable[Mapping[str, Any]],
    verdicts: Mapping[str, str],
    *,
    host: str,
    split: str,
    seed: int,
    run: str,
    phase: str,
    services: Mapping[str, str] | None = None,
    detection_ids: Sequence[str] = (),
) -> list[FailureObservation]:
    """φ's inputs for every failed task of a run, from the chaos proxy's ordered call log.

    ``services`` maps a service to its tool name (:func:`tool_names`); a call to a service with
    several tools is named ``<service>:<METHOD-less path>``."""
    names = dict(services or {})
    calls: dict[str, list[tuple[str, bool]]] = {}
    for row in rows:
        service, path = str(row["service"]), str(row["path"])
        tool = names.get(service) or f"{service}:{normalize(path)}"
        calls.setdefault(str(row["task"]), []).append((tool, int(row["status"]) >= 400))
    found = []
    for task, verdict in sorted(verdicts.items()):
        if verdict == "PASS":
            continue
        sequence = _with_retries(calls.get(task, []))
        tool = sequence[-1].removesuffix("#retry") if sequence else None
        found.append(FailureObservation(
            trace_id=f"local:{run}:{task}", host=host, failing_kind="CHAIN",
            failing_name=VERDICT_SPAN, tool_name=tool, error=verdict,
            preceding=tuple(sequence[-3:]), detection_ids=tuple(detection_ids), task_id=task,
            verdict=verdict,
            repro={"host": host, "task_id": task, "split": split, "seed": seed, "run": run,
                   "phase": phase},
        ))  # fmt: skip
    return found

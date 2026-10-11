"""Normalized traces and spans: what the Sentinel and the Pathologist read (spec §4.1.6, §5.7).

``load_trace`` joins three views of one trace: the summary (``GET /traces/{id}``: workflow,
session, tags, cost, latency, tool-call count), the span list (``/spans``: kind, model, tokens,
cost) and the raw spans (``/raw-spans``: input, output and error previews). Truncated previews
are completed from ``/spans/{id}/payload`` only when asked, because that endpoint's rate class
holds 10 requests per minute (spike A11).

Hosts emit one trace per task, ``task:<id>``, holding the agent's spans and its
``grader.verdict`` (A-28). From a trace Omega derives:

- the **verdicts** (``PASS`` / ``FAIL:<reason>``) that receptor R-007 matches;
- the ordered **tool exchanges** (name, arguments, result or error), the fidelity reference for
  a reproduction;
- a **repro spec** (host, task, split, seed, run): with deterministic per-seed chaos (A-24) the
  G0 cassette is re-recorded by re-running exactly that task at that seed. Span payloads hold
  tool-level arguments and return values, not the HTTP exchanges a cassette replays, so a
  cassette is never reconstructed from spans alone.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from kintsugi.senses.neatlogs_api import PayloadField, RawSpan, Span, TraceSummary
from kintsugi.senses.tags import TagSet

VERDICT_SPAN = "grader.verdict"
TASK_PREFIX = "task:"
HOST_PREFIX = "host:"


def decode(value: str | None, mime_type: str | None = None) -> Any:
    """A payload as JSON when it parses (span payloads are JSON-encoded), else the text."""
    if value is None:
        return None
    if mime_type is not None and "json" not in mime_type:
        return value
    try:
        return json.loads(value)
    except ValueError:
        return value


@dataclass(frozen=True)
class SpanRecord:
    trace_id: str
    span_id: str
    parent_id: str | None
    kind: str
    name: str
    status: str
    started_at: datetime
    ended_at: datetime | None
    duration_ms: float
    cost_usd: float
    model: str | None
    prompt_tokens: int
    completion_tokens: int
    input: Any
    output: Any
    error: str | None
    truncated: bool

    @property
    def failed(self) -> bool:
        return self.status.upper() not in {"OK", "UNSET"} or bool(self.error)


@dataclass(frozen=True)
class Verdict:
    task_id: str | None
    verdict: str
    span_id: str

    @property
    def passed(self) -> bool:
        return self.verdict == "PASS"

    @property
    def reason(self) -> str | None:
        return self.verdict.removeprefix("FAIL:") if self.verdict.startswith("FAIL:") else None


@dataclass(frozen=True)
class ToolExchange:
    name: str
    arguments: Any
    result: Any
    error: str | None
    status: str
    started_at: datetime


@dataclass(frozen=True)
class ReproSpec:
    host: str
    task_id: str
    split: str
    seed: int
    run: str | None
    phase: str | None


@dataclass(frozen=True)
class TraceRecord:
    trace_id: str
    name: str | None
    workflow: str | None
    session_id: str
    status: str
    started_at: datetime
    completed_at: datetime | None
    duration_ms: float
    cost_usd: float
    total_tokens: int
    tool_calls: int
    llm_calls: int
    span_count: int
    tags: TagSet
    revision: int | None
    spans: tuple[SpanRecord, ...] = ()

    @property
    def host(self) -> str | None:
        if self.workflow and self.workflow.startswith(HOST_PREFIX):
            return self.workflow.removeprefix(HOST_PREFIX)
        return self.tags.one("host")

    @property
    def task_id(self) -> str | None:
        if self.name and self.name.startswith(TASK_PREFIX):
            return self.name.removeprefix(TASK_PREFIX)
        verdicts = self.verdicts
        return verdicts[0].task_id if verdicts else None

    @property
    def verdicts(self) -> tuple[Verdict, ...]:
        found = (verdict_of(span) for span in self.spans)
        return tuple(v for v in found if v is not None)

    @property
    def failed(self) -> bool:
        return any(not v.passed for v in self.verdicts)

    def tool_exchanges(self) -> list[ToolExchange]:
        tools = sorted((s for s in self.spans if s.kind == "TOOL"), key=lambda s: s.started_at)
        return [ToolExchange(s.name, s.input, s.output, s.error, s.status, s.started_at)
                for s in tools]  # fmt: skip

    def repro(self) -> ReproSpec | None:
        """The exact task to re-run for G0, or ``None`` when the trace does not identify one."""
        host, task, split, seed = self.host, self.task_id, self.tags.one("split"), None
        raw_seed = self.tags.one("seed")
        if raw_seed is not None:
            seed = int(raw_seed)
        if host is None or task is None or split is None or seed is None:
            return None
        return ReproSpec(host, task, split, seed, self.tags.one("run"), self.tags.one("phase"))


def verdict_of(span: SpanRecord) -> Verdict | None:
    if span.name != VERDICT_SPAN:
        return None
    verdict = span.output
    if not isinstance(verdict, str) or not (verdict == "PASS" or verdict.startswith("FAIL:")):
        return None
    task = span.input.get("task_id") if isinstance(span.input, dict) else None
    return Verdict(task if isinstance(task, str) else None, verdict, span.span_id)


def summary_record(summary: TraceSummary, spans: Sequence[SpanRecord] = ()) -> TraceRecord:
    return TraceRecord(
        trace_id=summary.trace_id,
        name=summary.trace_name,
        workflow=summary.workflow_name,
        session_id=summary.session_id,
        status=summary.status,
        started_at=summary.started_at,
        completed_at=summary.completed_at,
        duration_ms=summary.duration_ms,
        cost_usd=summary.total_cost_usd,
        total_tokens=summary.total_tokens,
        tool_calls=summary.tool_calls_count,
        llm_calls=summary.llm_calls_count,
        span_count=summary.spans_count,
        tags=TagSet.parse(summary.tags),
        revision=summary.metadata_revision,
        spans=tuple(spans),
    )


def join_spans(
    spans: Iterable[Span],
    raw: Iterable[RawSpan],
    payloads: dict[str, dict[str, PayloadField]] | None = None,
) -> list[SpanRecord]:
    """One record per span id: kind/model/cost from ``spans``, I/O from ``raw`` (+ payloads)."""
    by_id = {s.span_id: s for s in raw}
    out: list[SpanRecord] = []
    for span in spans:
        r = by_id.get(span.span_id)
        full = (payloads or {}).get(span.span_id, {})
        if r is not None:
            in_text, in_mime = r.input.preview, r.input.mime_type
            out_text, out_mime = r.output.preview, r.output.mime_type
            truncated = r.input.truncated or r.output.truncated or r.error.truncated
            error = r.error.message_preview or None
        else:
            in_text = out_text = in_mime = out_mime = error = None
            truncated = True
        if "input" in full and full["input"].value is not None:
            in_text, in_mime = full["input"].value, full["input"].mime_type
        if "output" in full and full["output"].value is not None:
            out_text, out_mime = full["output"].value, full["output"].mime_type
        if "error" in full and full["error"].available and full["error"].value:
            error = full["error"].value
        if full:
            truncated = any(f.truncated for f in full.values())
        out.append(SpanRecord(
            trace_id=span.trace_id, span_id=span.span_id, parent_id=span.parent_span_id,
            kind=span.span_type.upper(), name=span.span_name, status=span.status,
            started_at=span.started_at, ended_at=span.ended_at, duration_ms=span.duration_ms,
            cost_usd=span.cost_usd, model=span.model, prompt_tokens=span.prompt_tokens,
            completion_tokens=span.completion_tokens, input=decode(in_text, in_mime),
            output=decode(out_text, out_mime), error=error, truncated=truncated,
        ))  # fmt: skip
    return out


class _Api(Protocol):
    def trace(self, trace_id: str) -> TraceSummary: ...

    def spans(self, trace_id: str) -> list[Span]: ...

    def raw_spans(self, trace_id: str) -> list[RawSpan]: ...

    def span_payload(
        self, trace_id: str, span_id: str, fields: Sequence[str] = ...
    ) -> dict[str, PayloadField]: ...


def load_trace(api: _Api, trace_id: str, *, complete_truncated: bool = False) -> TraceRecord:
    summary = api.trace(trace_id)
    spans = api.spans(trace_id)
    raw = api.raw_spans(trace_id)
    payloads: dict[str, dict[str, PayloadField]] = {}
    if complete_truncated:
        for r in raw:
            if r.input.truncated or r.output.truncated or r.error.truncated:
                payloads[r.span_id] = api.span_payload(trace_id, r.span_id)
    return summary_record(summary, join_spans(spans, raw, payloads))

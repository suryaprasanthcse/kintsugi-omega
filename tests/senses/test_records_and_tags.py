"""Trace normalization (verdicts, tool exchanges, repro spec) and the tag schema and writer."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from kintsugi.senses import tags as t
from kintsugi.senses.neatlogs_api import Conflict, PayloadField, RawSpan, Span, TraceSummary
from kintsugi.senses.records import load_trace

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "neatlogs"


def data(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))["body"]["data"]


class FixtureApi:
    """Serves the recorded train-split Ledger task trace."""

    def __init__(self) -> None:
        self.payload_calls: list[str] = []

    def trace(self, trace_id: str) -> TraceSummary:
        return TraceSummary.model_validate(data("trace_get"))

    def spans(self, trace_id: str) -> list[Span]:
        return [Span.model_validate(s) for s in data("trace_spans")["spans"]]

    def raw_spans(self, trace_id: str) -> list[RawSpan]:
        return [RawSpan.model_validate(s) for s in data("trace_raw_spans")["spans"]]

    def span_payload(
        self, trace_id: str, span_id: str, fields: Sequence[str] = ("input", "output", "error")
    ) -> dict[str, PayloadField]:
        self.payload_calls.append(span_id)
        recorded = data("span_payload_tool")["fields"]
        return {k: PayloadField.model_validate(v) for k, v in recorded.items()}


def test_a_task_trace_normalizes_to_host_task_verdict_and_repro() -> None:
    record = load_trace(FixtureApi(), "d23ffdad100e3a3bd55365ebc266447d")
    assert record.host == "ledger"
    assert record.task_id == "ledger-train-002"
    assert [v.verdict for v in record.verdicts] == ["PASS"]
    assert record.verdicts[0].task_id == "ledger-train-002"
    assert not record.failed
    repro = record.repro()
    assert repro is not None
    assert (repro.host, repro.task_id) == ("ledger", "ledger-train-002")
    assert (repro.split, repro.seed) == ("train", 7)
    assert repro.run == record.tags.one("run")
    kinds = {s.kind for s in record.spans}
    assert kinds == {"WORKFLOW", "LLM", "TOOL", "CHAIN"}


def test_tool_exchanges_keep_order_arguments_and_results() -> None:
    record = load_trace(FixtureApi(), "d23ffdad100e3a3bd55365ebc266447d")
    exchanges = record.tool_exchanges()
    names = [e.name for e in exchanges]
    assert names[0] == "parse_invoice"
    assert names[-1] == "reconcile_report"
    assert names.count("post_journal_entry") == 7
    fx = next(e for e in exchanges if e.name == "fx_rate")
    assert isinstance(fx.arguments, dict)
    assert [e.started_at for e in exchanges] == sorted(e.started_at for e in exchanges)


def test_truncated_previews_are_completed_only_on_request() -> None:
    api = FixtureApi()
    load_trace(api, "t")
    assert api.payload_calls == []  # the payload class allows 10 requests per minute
    load_trace(api, "t", complete_truncated=True)
    raw = data("trace_raw_spans")["spans"]
    truncated = [s["spanId"] for s in raw if s["input"]["truncated"] or s["output"]["truncated"]]
    assert api.payload_calls == truncated


def test_a_fail_verdict_is_read_from_its_json_encoded_output() -> None:
    from kintsugi.senses.records import SpanRecord, verdict_of

    base = load_trace(FixtureApi(), "t").spans
    grader = next(s for s in base if s.name == "grader.verdict")
    failing = SpanRecord(**{**grader.__dict__, "output": "FAIL:DUPLICATE_ENTRY"})
    verdict = verdict_of(failing)
    assert verdict is not None
    assert not verdict.passed
    assert verdict.reason == "DUPLICATE_ENTRY"
    assert verdict_of(SpanRecord(**{**grader.__dict__, "output": "ok"})) is None


# --------------------------------------------------------------------------- tags


@pytest.mark.parametrize(
    "tag",
    ["host:ledger", "inc:INC-0042", "phase:canary", "hyp:H3", "ab:AB-17@1.1", "split:hold",
     "split:holdout", "split:live-1000004", "seed:3", "gate:G4-pass", "run:1353c16b"],
)  # fmt: skip
def test_spec_tags_validate(tag: str) -> None:
    assert t.validate(tag) == tag


@pytest.mark.parametrize(
    "tag",
    ["phase:deploy", "hyp:X1", "ab:AB-17", "gate:G9-pass", "run:XYZ", "colour:red", "host:", "",
     "inc:" + "A" * 200],
)  # fmt: skip
def test_malformed_tags_are_refused(tag: str) -> None:
    with pytest.raises(t.TagError):
        t.validate(tag)


def test_merge_replaces_single_valued_keys_and_keeps_foreign_tags() -> None:
    current = ["host:ledger", "phase:baseline", "hyp:H1", "team-label"]
    merged = t.merge(current, add=["phase:repro", "hyp:H2", "inc:INC-0042"])
    assert merged[:3] == ["host:ledger", "hyp:H1", "team-label"]
    assert merged[3:] == ["phase:repro", "hyp:H2", "inc:INC-0042"]
    assert t.merge(merged, remove=["hyp:H1"]).count("hyp:H1") == 0


def test_merge_refuses_to_exceed_ten_tags() -> None:
    current = [f"hyp:H{i}" for i in range(1, 11)]
    with pytest.raises(t.TagOverflow):
        t.merge(current, add=["phase:repro"])


def test_tag_sets_parse_known_keys_and_keep_the_rest() -> None:
    parsed = t.TagSet.parse(["host:ledger", "seed:7", "phase:spike", "hyp:H1", "hyp:H2"])
    assert parsed.one("host") == "ledger"
    assert parsed.all("hyp") == ("H1", "H2")
    assert parsed.other == ("phase:spike",)


class TagApi:
    def __init__(self, conflicts: int = 0) -> None:
        self.tags: tuple[str, ...] = ("host:ledger", "phase:baseline")
        self.revision = 3
        self.conflicts = conflicts
        self.patches: list[tuple[int, tuple[str, ...], str | None]] = []

    def _summary(self) -> TraceSummary:
        base = data("trace_get")
        return TraceSummary.model_validate({**base, "tags": list(self.tags),
                                            "metadataRevision": self.revision})  # fmt: skip

    def trace(self, trace_id: str) -> TraceSummary:
        return self._summary()

    def patch_trace(
        self,
        trace_id: str,
        *,
        expected_revision: int,
        tags: Sequence[str] | None = None,
        trace_name: str | None = None,
        idempotency_key: str | None = None,
    ) -> TraceSummary:
        self.patches.append((expected_revision, tuple(tags or ()), idempotency_key))
        if self.conflicts:
            self.conflicts -= 1
            self.revision += 1  # someone else wrote first
            self.tags = (*self.tags, "foreign")
            raise Conflict("traces.patch", 409, "CONFLICT", "revision moved")
        assert expected_revision == self.revision
        self.tags = tuple(tags or ())
        self.revision += 1
        return self._summary()


def test_the_writer_reads_modifies_and_writes_with_the_revision() -> None:
    api = TagApi()
    result = t.TagWriter(api).apply("tr", add=["phase:repro", "inc:INC-0042"])
    assert result.tags == ("host:ledger", "phase:repro", "inc:INC-0042")
    assert api.patches[0][0] == 3


def test_the_writer_rereads_after_a_conflict_and_keeps_the_other_writer_s_tag() -> None:
    api = TagApi(conflicts=1)
    writer = t.TagWriter(api)
    result = writer.apply("tr", add=["phase:repro"])
    assert writer.conflicts == 1
    assert "foreign" in result.tags
    assert "phase:repro" in result.tags
    (rev1, _, key1), (rev2, _, key2) = api.patches
    assert (rev1, rev2) == (3, 4)
    assert key1 != key2  # a write against a newer revision is a new logical write


def test_a_no_op_merge_writes_nothing() -> None:
    api = TagApi()
    t.TagWriter(api).apply("tr", add=["host:ledger"])
    assert api.patches == []


def test_the_idempotency_key_is_stable_for_the_same_write() -> None:
    a = t.idempotency_key("tr", 3, ["host:ledger"])
    # Accepted formats: UUIDv4 or unpadded base64url of 128/256 bits (a UUIDv5 was refused live).
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", a)
    assert a == t.idempotency_key("tr", 3, ["host:ledger"])
    assert a != t.idempotency_key("tr", 4, ["host:ledger"])

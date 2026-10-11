"""Crew foundations: pinned templates, context manifests, the model port, the case file."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from kintsugi.crew import claude_session as cs
from kintsugi.crew import templates
from kintsugi.crew.casefile import KINDS, READS, CaseFile, InformationFlowViolation
from kintsugi.crew.consult import DETERMINISTIC, consult
from kintsugi.crew.llm import (
    ClaudeCliModel,
    ModelCall,
    ModelError,
    ProxyModel,
    ScriptedModel,
    parse_json,
    validated,
)
from kintsugi.crew.manifest import BudgetExhausted, ContextBuilder, estimate_tokens
from kintsugi.sandbox import llm_proxy as lp
from kintsugi.sandbox.httpserve import Server

SCHEMA = {"type": "object", "properties": {"severity": {"type": "string"}},
          "required": ["severity"], "additionalProperties": False}  # fmt: skip


# ---- templates ------------------------------------------------------------------------------


def test_every_template_is_pinned_and_unchanged() -> None:
    assert templates.check_pins() == []
    assert set(templates.pins()) == set(templates.names())
    assert {"sentinel", "pathologist", "archivist", "coordinator", "surgeon",
            "auditor_g5"} <= set(templates.names())  # fmt: skip


def test_rendering_is_strict_about_fields() -> None:
    fields = templates.load("sentinel").fields
    values = {name: f"<{name}>" for name in fields}
    rendered = templates.render("sentinel", **values)
    assert "$" not in rendered.prompt
    assert rendered.prompt_sha == templates.sha256_text(rendered.prompt)
    assert rendered.template_sha == templates.load("sentinel").sha256
    with pytest.raises(templates.TemplateError, match="missing"):
        templates.render("sentinel", **{k: v for k, v in values.items() if k != "host"})
    with pytest.raises(templates.TemplateError, match="unknown"):
        templates.render("sentinel", **values, extra="x")


def test_the_surgeon_prompt_carries_the_nonce_verbatim() -> None:
    fields = dict.fromkeys(templates.load("surgeon").fields, "x")
    nonce = "alpha-bravo-charlie-delta-echo-fox-golf-hotel-india-juliet-kilo"
    rendered = templates.render("surgeon", **{**fields, "nonce": nonce})
    assert f"Session nonce: {nonce}" in rendered.prompt  # audit C4 finds it in the transcript


# ---- manifests ------------------------------------------------------------------------------


def test_the_builder_records_inclusions_exclusions_and_truncation() -> None:
    builder = ContextBuilder("sentinel", "INC-0001", budget_tokens=100)
    builder.add("hits", "h" * 200, source="neatlogs hits", why="the alert")
    builder.add("rule", "r" * 1000, source="receptor rule", why="threshold")  # does not fit
    builder.exclude("source code", "Sentinel never reads code")
    manifest = builder.manifest
    assert [item.source for item in manifest.included] == ["neatlogs hits", "receptor rule"]
    assert manifest.included[1].truncated
    assert manifest.tokens_used <= manifest.budget_tokens + estimate_tokens("[... truncated")
    assert manifest.excluded[0].why == "Sentinel never reads code"
    with pytest.raises(BudgetExhausted):
        builder.add("more", "x" * 400, source="more", why="over")
    builder.add("optional", "x" * 400, source="optional", why="nice", required=False)
    assert builder.manifest.excluded[-1].source == "optional"


# ---- model port -----------------------------------------------------------------------------


def test_parse_and_validate_replies() -> None:
    assert parse_json('```json\n{"severity": "high"}\n```') == {"severity": "high"}
    assert parse_json('Here: {"severity": "low"} done') == {"severity": "low"}
    with pytest.raises(ModelError):
        parse_json("no json here")
    with pytest.raises(ModelError, match="schema"):
        validated({"severity": 3}, SCHEMA)


def test_the_cli_provider_disables_tools_and_asks_for_the_schema(tmp_path: Path) -> None:
    call = ModelCall("sentinel", "sentinel", "SYSTEM", "PROMPT", SCHEMA, max_budget_usd=0.05)
    request = ClaudeCliModel().request(call, tmp_path)
    argv = cs.build_argv(request)
    assert request.model == "claude-haiku-5-5"
    assert argv[argv.index("--tools") + 1] == ""
    assert "--allowedTools" not in argv
    assert json.loads(argv[argv.index("--json-schema") + 1]) == SCHEMA
    assert argv[argv.index("--system-prompt") + 1] == "SYSTEM"
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert "--no-session-persistence" in argv
    assert "PROMPT" not in argv  # the prompt goes in on stdin
    override = ClaudeCliModel(overrides={"sentinel": "claude-sonnet-5-5"}).request(call, tmp_path)
    assert override.model == "claude-sonnet-5-5"


FAKE = r"""
import json, sys
prompt = sys.stdin.buffer.read().decode()
print(json.dumps({"type": "system", "subtype": "init", "session_id": "s-1"}), flush=True)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "total_cost_usd": 0.004, "result": "{\"severity\": \"high\"}",
                  "structured_output": {"severity": "high"}, "session_id": "s-1"}), flush=True)
"""


def test_the_cli_provider_reads_the_structured_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "fake.py"
    script.write_text(FAKE, encoding="utf-8")
    original = cs.build_argv
    monkeypatch.setattr(cs, "build_argv", lambda r: [sys.executable, str(script),
                                                     *original(r)[1:]])  # fmt: skip
    reply = ClaudeCliModel().complete(ModelCall("sentinel", "sentinel", "S", "P", SCHEMA))
    assert reply.data == {"severity": "high"}
    assert reply.cost_usd == pytest.approx(0.004)
    assert reply.provider == "claude-cli"
    pids: list[int] = []
    request = cs.SessionRequest(prompt="P", cwd=tmp_path, allowed_tools=())
    assert cs.run(request, on_start=pids.append).ok
    assert len(pids) == 1  # the Surgeon step writes it into the worktree lock
    assert pids[0] > 0


def test_the_proxy_provider_goes_through_the_metering_proxy() -> None:
    account = "omega-crew"
    spend = lp.SpendLedger(":memory:", daily_cap_usd=20.0,
                           limits={lp.account_of(account): lp.AccountLimits(5.0)})  # fmt: skip
    seen: list[dict[str, Any]] = []

    def responder(body: dict[str, Any]) -> dict[str, Any]:
        seen.append(body)
        return {"content": [{"type": "text", "text": '{"severity": "warning"}'}]}

    token = "kintsugi-proxy-omega-crew-0001"
    proxy = lp.LlmProxy(lp.OfflineUpstream({account: responder}), spend, {account: token})
    with Server(proxy) as server:
        reply = ProxyModel(f"{server.url}/h/{account}", token).complete(
            ModelCall("sentinel", "sentinel", "SYS", "PROMPT", SCHEMA)
        )
    assert reply.data == {"severity": "warning"}
    assert seen[0]["model"] == "claude-haiku-5-5"
    assert seen[0]["system"] == "SYS"
    assert "JSON Schema" in seen[0]["messages"][0]["content"]
    assert spend.spent(account=lp.account_of(account), billable=None) > 0  # metered (D7)


# ---- case file and the information-flow matrix --------------------------------------------


def test_every_artifact_kind_maps_to_a_matrix_column() -> None:
    columns = set(KINDS.values())
    assert columns <= {c for row in READS.values() for c in row} | {"reasoning"}


def test_the_auditor_cannot_read_surgeon_output(tmp_path: Path) -> None:
    case = CaseFile(tmp_path, "INC-0001")
    case.put("surgeon_report", "H1", {"report": "trust me, it works"}, by="surgeon")
    case.put("outcome", "H1", {"commit": "abc"}, by="surgeon")
    auditor = case.reader("auditor")
    assert auditor.get("outcome", "H1") == {"commit": "abc"}
    for kind in ("surgeon_report", "surgeon_session", "plan"):
        with pytest.raises(InformationFlowViolation):
            auditor.get(kind, "H1")
    denied = [a for a in case.accesses() if a["op"] == "denied"]
    assert {a["kind"] for a in denied} == {"surgeon_report", "surgeon_session", "plan"}


def test_writes_follow_the_matrix_too(tmp_path: Path) -> None:
    case = CaseFile(tmp_path, "INC-0002")
    case.put("incident_open", "open", {"host": "ledger"}, by="sentinel")
    with pytest.raises(InformationFlowViolation):
        case.put("verdict", "H1", {"passed": True}, by="sentinel")
    with pytest.raises(InformationFlowViolation):
        case.put("verdict", "H1", {"passed": True}, by="surgeon")
    with pytest.raises(InformationFlowViolation):  # no role reads a Surgeon's budget
        case.reader("surgeon").get("plan", "plan")


def test_consult_logs_the_call_meters_it_and_falls_back(tmp_path: Path) -> None:
    case = CaseFile(tmp_path, "INC-0003")
    fields = templates.load("sentinel").fields
    builder = ContextBuilder("sentinel", "INC-0003")
    for name in sorted(fields):
        builder.add(name, f"<{name}>", source=name, why="test")
    model = ScriptedModel({"sentinel": {"severity": "high"}}, cost_usd=0.01)
    result = consult(model, case, builder, "sentinel", schema=SCHEMA)
    assert result.data == {"severity": "high"}
    assert case.spent() == pytest.approx(0.01)
    call = case.calls()[0]
    assert call["template_sha"] == templates.load("sentinel").sha256
    assert call["manifest"]["prompt_sha"] == call["prompt_sha"]

    def broken(_call: ModelCall) -> Any:
        raise ModelError("down")

    fallback = consult(ScriptedModel({"sentinel": broken}), case, builder, "sentinel",
                       schema=SCHEMA, fallback=lambda: {"severity": "low"})  # fmt: skip
    assert (fallback.data, fallback.provider) == ({"severity": "low"}, DETERMINISTIC)
    assert case.calls()[-1]["error"] == "down"
    offline = consult(None, case, builder, "sentinel", schema=SCHEMA,
                      fallback=lambda: {"severity": "warning"})  # fmt: skip
    assert offline.provider == DETERMINISTIC

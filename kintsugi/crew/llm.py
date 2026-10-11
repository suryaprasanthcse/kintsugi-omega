"""The crew's model port (spec §5.7; rulings D7 and A-23; Stage 6 Architect decision 1).

Every crew role except the Surgeon asks one structured question per call: a system prompt, a
rendered user prompt (``kintsugi.crew.templates``) and a JSON schema for the answer. Three
providers answer it:

- :class:`ClaudeCliModel`: the logged-in headless Claude Code CLI (``claude -p``), with every
  tool disabled, no settings or session files of its own, ``--json-schema`` for the answer and
  ``--max-budget-usd`` per call. This is the live provider while ``ANTHROPIC_API_KEY`` is absent.
- :class:`ProxyModel`: the Messages API through Omega's LLM metering proxy
  (``kintsugi.sandbox.llm_proxy``), which enforces the $20/day cap and meters cost per account
  (ruling D7). It is the path once the key exists; until then it is exercised against the
  proxy's offline upstream.
- :class:`ScriptedModel`: deterministic answers keyed by template, for tests and labelled
  offline runs. Roles that have a deterministic fallback (the Sentinel's severity, the
  Pathologist's summary, the Archivist's quote extraction, the Auditor's revert check) use it
  when no model is configured.

Every answer is validated against its schema before a role uses it.
"""

from __future__ import annotations

import json
import re
import tempfile
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import jsonschema

from kintsugi.crew import claude_session

# Spec §5.7 / §11 model per role.
ROLE_MODELS: Mapping[str, str] = {
    "sentinel": "claude-haiku-5-5",
    "pathologist": "claude-opus-5-5",
    "archivist": "claude-opus-5-5",
    "coordinator": "claude-opus-5-5",
    "auditor": "claude-opus-5-5",
    "surgeon": "claude-sonnet-5-5",
}


class ModelError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelCall:
    role: str
    template: str
    system: str
    prompt: str
    schema: Mapping[str, Any] | None = None
    model: str | None = None  # None: the role's model (ROLE_MODELS)
    max_budget_usd: float = 1.0
    timeout_s: float = 900.0

    @property
    def model_id(self) -> str:
        return self.model or ROLE_MODELS[self.role]


@dataclass(frozen=True)
class ModelReply:
    text: str
    data: Any
    cost_usd: float
    model: str
    provider: str
    session_id: str | None = None


class Model(Protocol):
    provider: str

    def complete(self, call: ModelCall) -> ModelReply: ...


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_json(text: str) -> Any:
    """The JSON value in a reply: the whole text, else the first ``{…}`` object in it."""
    stripped = _FENCE.sub("", text.strip()).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start, end = stripped.find("{"), stripped.rfind("}")
        if start < 0 or end <= start:
            raise ModelError(f"no JSON object in the reply: {text[:200]!r}") from None
        try:
            return json.loads(stripped[start : end + 1])
        except json.JSONDecodeError as error:
            raise ModelError(f"malformed JSON in the reply: {error}") from None


def validated(data: Any, schema: Mapping[str, Any] | None) -> Any:
    if schema is not None:
        try:
            jsonschema.validate(data, dict(schema))
        except jsonschema.ValidationError as error:
            raise ModelError(f"the reply does not match its schema: {error.message}") from None
    return data


class ClaudeCliModel:
    """``claude -p`` with no tools, in an empty directory, one structured answer per call."""

    provider = "claude-cli"

    def __init__(
        self,
        *,
        overrides: Mapping[str, str] | None = None,  # role → model (e.g. usage-limited runs)
        extra_env: Mapping[str, str] | None = None,
    ) -> None:
        self.overrides = dict(overrides or {})
        self.extra_env = dict(extra_env or {})

    def request(self, call: ModelCall, cwd: Path) -> claude_session.SessionRequest:
        return claude_session.SessionRequest(
            prompt=call.prompt,
            cwd=cwd,
            allowed_tools=(),
            model=self.overrides.get(call.role, call.model_id),
            max_budget_usd=call.max_budget_usd,
            timeout_s=call.timeout_s,
            extra_env=self.extra_env,
            tools=(),
            system_prompt=call.system,
            json_schema=call.schema,
            setting_sources="",
            persist=False,
        )

    def complete(self, call: ModelCall) -> ModelReply:
        with tempfile.TemporaryDirectory(prefix="kintsugi-crew-") as scratch:
            request = self.request(call, Path(scratch))
            result = claude_session.run(request)
        stream = result.stream
        if not result.ok or stream.result is None:
            reason = stream.error or (stream.result or {}).get("subtype") or result.exit_code
            detail = str((stream.result or {}).get("result") or "")[:300]  # e.g. an API error
            raise ModelError(f"{call.role}/{call.template}: claude -p failed ({reason}) {detail}")
        structured = stream.result.get("structured_output")
        text = str(stream.result.get("result") or "")
        data = structured if structured is not None else (parse_json(text) if call.schema else None)
        return ModelReply(text, validated(data, call.schema), stream.cost_usd, request.model,
                          self.provider, result.session_id)  # fmt: skip


class ProxyModel:
    """The Messages API through the LLM metering proxy (ruling D7: daily cap, per-account cost).

    ``base_url`` is the proxy's account URL (``<proxy>/h/<account>``) and ``token`` the proxy
    placeholder token for that account; the real key lives only in the proxy."""

    provider = "anthropic-proxy"

    def __init__(self, base_url: str, token: str, *, max_tokens: int = 4096) -> None:
        self.base_url = base_url
        self.token = token
        self.max_tokens = max_tokens

    def complete(self, call: ModelCall) -> ModelReply:
        import anthropic

        from kintsugi.sandbox.llm_proxy import COST_HEADER

        client = anthropic.Anthropic(base_url=self.base_url, api_key=self.token, max_retries=2)
        prompt = call.prompt
        if call.schema is not None:
            prompt += ("\n\nAnswer with one JSON object matching this JSON Schema, and nothing "
                       "else:\n" + json.dumps(dict(call.schema), sort_keys=True))  # fmt: skip
        try:
            raw = client.messages.with_raw_response.create(
                model=call.model_id,
                max_tokens=self.max_tokens,
                system=call.system,
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.APIStatusError as error:
            status = error.status_code
            raise ModelError(f"{call.role}/{call.template}: proxy refused ({status})") from None
        message = raw.parse()
        text = "".join(getattr(block, "text", "") for block in message.content)
        data = validated(parse_json(text), call.schema) if call.schema else None
        cost = float(raw.headers.get(COST_HEADER) or 0.0)
        return ModelReply(text, data, cost, call.model_id, self.provider, message.id)


Answer = Callable[[ModelCall], Any]


@dataclass
class ScriptedModel:
    """Answers keyed by template name: a value, or a function of the call."""

    answers: Mapping[str, Any]
    cost_usd: float = 0.0
    provider: str = "scripted"
    calls: list[ModelCall] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def complete(self, call: ModelCall) -> ModelReply:
        with self._lock:
            self.calls.append(call)
        if call.template not in self.answers:
            raise ModelError(f"the scripted model has no answer for {call.template!r}")
        answer = self.answers[call.template]
        data = answer(call) if callable(answer) else answer
        text = data if isinstance(data, str) else json.dumps(data)
        parsed = validated(data, call.schema) if call.schema is not None else None
        return ModelReply(text, parsed, self.cost_usd, call.model_id, self.provider,
                          f"scripted-{uuid.uuid4().hex[:8]}")  # fmt: skip

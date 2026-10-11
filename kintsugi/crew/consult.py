"""One crew model call, end to end: template → manifest → model → validation → case file.

``consult`` is the only way a role asks a model anything, so every call is rendered from a
pinned template, carries its context manifest (§5.14), is metered into the incident's cost log
(G6) and is recorded in ``calls.jsonl`` with its template and prompt hashes. That log is also
what the I5 leak scan reads: no holdout canary may ever appear in a crew prompt or reply.

When no model is configured (offline runs, unit tests) or the model fails, a role's
deterministic fallback answers instead, and the call record says so.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from kintsugi.crew import templates
from kintsugi.crew.casefile import CaseFile
from kintsugi.crew.llm import Model, ModelCall, ModelError, ModelReply
from kintsugi.crew.manifest import ContextBuilder, ContextManifest

DETERMINISTIC = "deterministic"


@dataclass(frozen=True)
class Consultation:
    data: Any
    provider: str
    model: str | None
    cost_usd: float
    manifest: ContextManifest
    error: str | None = None


def consult(
    model: Model | None,
    case: CaseFile,
    builder: ContextBuilder,
    template: str,
    *,
    schema: Mapping[str, Any] | None,
    fallback: Callable[[], Any] | None = None,
    max_budget_usd: float = 1.0,
    model_id: str | None = None,
) -> Consultation:
    role = builder.manifest.role
    rendered = templates.render(template, **builder.values)
    manifest = builder.manifest
    manifest.template, manifest.template_sha = template, rendered.template_sha
    manifest.prompt_sha = rendered.prompt_sha
    reply: ModelReply | None = None
    error: str | None = None
    if model is not None:
        call = ModelCall(role, template, rendered.system, rendered.prompt, schema, model_id,
                         max_budget_usd)  # fmt: skip
        try:
            reply = model.complete(call)
        except ModelError as failure:
            if fallback is None:
                _log(case, role, rendered, manifest, None, None, str(failure))
                raise
            error = str(failure)
    if reply is not None:
        data, provider, used, cost = reply.data, reply.provider, reply.model, reply.cost_usd
    elif fallback is not None:
        data, provider, used, cost = fallback(), DETERMINISTIC, None, 0.0
    else:
        raise ModelError(f"{role}/{template}: no model configured and no deterministic fallback")
    _log(case, role, rendered, manifest, reply, data, error)
    if cost:
        case.add_cost(role, cost, f"{template} via {provider}")
    return Consultation(data, provider, used, cost, manifest, error)


def _log(
    case: CaseFile,
    role: str,
    rendered: templates.Rendered,
    manifest: ContextManifest,
    reply: ModelReply | None,
    data: Any,
    error: str | None,
) -> None:
    case.log_call({
        "role": role,
        "template": rendered.template,
        "template_sha": rendered.template_sha,
        "prompt_sha": rendered.prompt_sha,
        "prompt": rendered.prompt,
        "manifest": manifest.as_dict(),
        "provider": reply.provider if reply else DETERMINISTIC,
        "model": reply.model if reply else None,
        "session": reply.session_id if reply else None,
        "cost_usd": reply.cost_usd if reply else 0.0,
        "reply": reply.text if reply else None,
        "data": data,
        "error": error,
    })  # fmt: skip

"""The pinned neatlogs Public API contract (spec Appendix C; risk R11: thin adapters + tests).

neatlogs publishes an OpenAPI 3.1 document for the Public API (285 operations, about 7 MB) at
``OPENAPI_URL``. Omega pins only what it calls: each operation in ``OPERATIONS`` with its inline
schemas, plus every component those reference, in ``contract/public-api.subset.json``.
``contract/PIN.json`` records where and when the full document was read and its SHA-256.

Offline, the client's request bodies and the golden response fixtures are validated against the
pin. The live contract test re-extracts the subset from the published document and fails on any
drift, so a vendor change surfaces as a failing test instead of a silent mis-parse.

    python -m kintsugi.senses.contract pin <openapi.json>     # (re)write the pin from a download
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

OPENAPI_URL = "https://docs.neatlogs.com/openapi/public-api.json"
PREFIX = "/api/v1/public"
CONTRACT_DIR = Path(__file__).with_name("contract")
SUBSET_PATH = CONTRACT_DIR / "public-api.subset.json"
PIN_PATH = CONTRACT_DIR / "PIN.json"
_PIN_URI = "urn:kintsugi:neatlogs-public-api"

# name -> (method, path below PREFIX). Every call the client makes is listed here.
OPERATIONS: Mapping[str, tuple[str, str]] = {
    "project.get": ("get", "/project"),
    "traces.list": ("get", "/traces"),
    "traces.get": ("get", "/traces/{traceId}"),
    "traces.patch": ("patch", "/traces/{traceId}"),
    "traces.spans": ("get", "/traces/{traceId}/spans"),
    "traces.span": ("get", "/traces/{traceId}/spans/{spanId}"),
    "traces.span_payload": ("get", "/traces/{traceId}/spans/{spanId}/payload"),
    "traces.raw_spans": ("get", "/traces/{traceId}/raw-spans"),
    "traces.export": ("get", "/traces/{traceId}/export"),
    "traces.sharing": ("get", "/traces/{traceId}/sharing"),
    "traces.evaluations": ("get", "/traces/{traceId}/evaluations"),
    "traces.feedback": ("post", "/traces/{traceId}/feedback"),
    "traces.span_feedback": ("put", "/traces/{traceId}/spans/{spanId}/feedback"),
    "traces.comment_threads": ("get", "/traces/{traceId}/comment-threads"),
    "traces.comment_message": ("post", "/traces/{traceId}/comment-threads/{threadId}/messages"),
    "search.traces": ("post", "/search/traces"),
    "search.semantic": ("post", "/search/traces/semantic"),
    "sessions.list": ("get", "/sessions"),
    "sessions.get": ("get", "/sessions/{sessionId}"),
    "sessions.export": ("get", "/sessions/{sessionId}/export"),
    "detection_groups.list": ("get", "/detection-groups"),
    "detection_groups.create": ("post", "/detection-groups"),
    "detections.list": ("get", "/detections"),
    "detections.create": ("post", "/detections"),
    "detections.get": ("get", "/detections/{detectionId}"),
    "detections.update": ("patch", "/detections/{detectionId}"),
    "detections.set_status": ("patch", "/detections/{detectionId}/status"),
    "detections.hits": ("get", "/detections/{detectionId}/hits"),
    "detections.trends": ("get", "/detections/{detectionId}/trends"),
    "detections.history": ("get", "/detections/{detectionId}/history"),
    "detections.backfill": ("post", "/detections/{detectionId}/backfills"),
    "detections.backfill_job": ("get", "/detections/{detectionId}/backfills/{jobId}"),
    "detections.generate": ("post", "/detections/generate"),
    "detections.preview": ("post", "/detections/preview"),
    "evaluators.list": ("get", "/evaluators"),
    "evaluators.run": ("post", "/evaluators/{evaluatorId}/runs"),
    "evaluators.run_get": ("get", "/evaluators/{evaluatorId}/runs/{runId}"),
    "webhooks.list": ("get", "/webhooks"),
    "webhooks.create": ("post", "/webhooks"),
    "webhooks.get": ("get", "/webhooks/{webhookId}"),
    "webhooks.update": ("patch", "/webhooks/{webhookId}"),
    "webhooks.set_status": ("post", "/webhooks/{webhookId}/status"),
    "webhooks.test": ("post", "/webhooks/{webhookId}/test"),
    "webhooks.deliveries": ("get", "/webhooks/{webhookId}/deliveries"),
    "webhooks.replay": ("post", "/webhooks/{webhookId}/deliveries/{deliveryId}/replay"),
    "alert_rules.list": ("get", "/alert-rules"),
    "alert_rules.create": ("post", "/alert-rules"),
    "alert_rules.get": ("get", "/alert-rules/{alertRuleId}"),
    "alert_rules.set_status": ("post", "/alert-rules/{alertRuleId}/status"),
    "alert_history.list": ("get", "/alert-history"),
}


class ContractError(ValueError):
    """A payload does not match the pinned contract, or an operation is not pinned."""


@dataclass(frozen=True)
class Operation:
    name: str
    method: str
    path: str  # below PREFIX, with {placeholders}
    operation_id: str
    security: tuple[str, ...]  # accepted schemes, e.g. ("ServiceAccountBearerAuth", "OAuth2")
    gate: str  # x-neatlogs-operation-gate: always | public-api-mutations | tier-c-compute | …
    idempotency_key: bool  # the operation requires an Idempotency-Key header

    @property
    def oauth_only(self) -> bool:
        return self.security == ("OAuth2",)


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _resolve_pointer(doc: Mapping[str, Any], ref: str) -> Any:
    node: Any = doc
    for part in ref.removeprefix("#/").split("/"):
        node = node[part.replace("~1", "/").replace("~0", "~")]
    return node


def _refs(node: Any) -> Iterator[str]:
    if isinstance(node, Mapping):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/"):
            yield ref
        for value in node.values():
            yield from _refs(value)
    elif isinstance(node, list):
        for value in node:
            yield from _refs(value)


def subset(full: Mapping[str, Any], operations: Mapping[str, tuple[str, str]] = OPERATIONS) -> dict:
    """The pinned view of ``full``: the listed operations and the components they reference."""
    paths: dict[str, dict[str, Any]] = {}
    for name, (method, path) in sorted(operations.items()):
        item = full["paths"].get(PREFIX + path)
        if item is None or method not in item:
            raise ContractError(f"{name}: {method.upper()} {PREFIX + path} is not in the document")
        paths.setdefault(PREFIX + path, {})[method] = copy.deepcopy(item[method])
    components: dict[str, dict[str, Any]] = {
        "securitySchemes": copy.deepcopy(full["components"]["securitySchemes"])
    }
    pending = sorted(set(_refs(paths)))
    seen: set[str] = set()
    while pending:
        ref = pending.pop()
        if ref in seen:
            continue
        seen.add(ref)
        parts = ref.removeprefix("#/").split("/")
        if parts[0] != "components" or len(parts) != 3:
            raise ContractError(f"unsupported reference {ref}")
        target = copy.deepcopy(_resolve_pointer(full, ref))
        components.setdefault(parts[1], {})[parts[2]] = target
        pending.extend(sorted(set(_refs(target)) - seen))
    for section in components.values():
        for key in list(section):
            section[key] = section.pop(key)
    return {
        "openapi": full["openapi"],
        "info": {"title": full["info"]["title"], "version": full["info"]["version"]},
        "paths": dict(sorted(paths.items())),
        "components": {k: dict(sorted(v.items())) for k, v in sorted(components.items())},
    }


@cache
def pinned() -> dict[str, Any]:
    return json.loads(SUBSET_PATH.read_text(encoding="utf-8"))


@cache
def pin_record() -> dict[str, Any]:
    return json.loads(PIN_PATH.read_text(encoding="utf-8"))


def _resolve(doc: Mapping[str, Any], node: Any) -> Any:
    while isinstance(node, Mapping) and "$ref" in node:
        node = _resolve_pointer(doc, node["$ref"])
    return node


@cache
def operation(name: str) -> Operation:
    if name not in OPERATIONS:
        raise ContractError(f"operation {name!r} is not pinned")
    method, path = OPERATIONS[name]
    op = pinned()["paths"][PREFIX + path][method]
    params = [_resolve(pinned(), p) for p in op.get("parameters", [])]
    idempotent = any(
        p.get("in") == "header"
        and p.get("name", "").lower() == "idempotency-key"
        and p.get("required")
        for p in params
    )
    security = tuple(scheme for requirement in op.get("security", []) for scheme in requirement)
    return Operation(name, method, path, op["operationId"], security,
                     op.get("x-neatlogs-operation-gate", "always"), idempotent)  # fmt: skip


def _locate(name: str, *keys: str) -> str:
    """The JSON pointer of ``keys`` below an operation, following ``$ref`` hops on the way."""
    method, path = OPERATIONS[name]
    pointer = "/".join(["#", "paths", _escape(PREFIX + path), method])
    node: Any = _resolve_pointer(pinned(), pointer)
    for key in keys:
        while isinstance(node, Mapping) and "$ref" in node:
            pointer = node["$ref"]
            node = _resolve_pointer(pinned(), pointer)
        if not isinstance(node, Mapping) or key not in node:
            raise KeyError(key)
        pointer, node = f"{pointer}/{_escape(key)}", node[key]
    return pointer


@cache
def _registry() -> Registry:
    resource = Resource.from_contents(pinned(), default_specification=DRAFT202012)
    return Registry().with_resource(_PIN_URI, resource)


def _validator(pointer: str) -> Draft202012Validator:
    schema = {"$ref": _PIN_URI + pointer}
    return Draft202012Validator(schema, registry=_registry())


def _check(validator: Draft202012Validator, value: Any, what: str) -> None:
    errors = sorted(validator.iter_errors(value), key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        where = "/".join(str(p) for p in first.absolute_path) or "<root>"
        raise ContractError(f"{what}: {where}: {first.message[:300]}")


def validate_response(name: str, status: int, body: Any) -> None:
    """Raise ``ContractError`` unless ``body`` matches the pinned JSON response for ``status``."""
    try:
        pointer = _locate(name, "responses", str(status), "content", "application/json", "schema")
    except KeyError as missing:
        raise ContractError(f"{name}: no pinned JSON response for HTTP {status}") from missing
    _check(_validator(pointer), body, f"{name} {status} response")


def validate_request(name: str, body: Any) -> None:
    """Raise ``ContractError`` unless ``body`` matches the pinned JSON request body."""
    try:
        pointer = _locate(name, "requestBody", "content", "application/json", "schema")
    except KeyError as missing:
        raise ContractError(f"{name}: the pinned operation takes no JSON body") from missing
    _check(_validator(pointer), body, f"{name} request")


def drift(live_full: Mapping[str, Any]) -> list[str]:
    """Pinned operations whose live definition differs (empty when the pin still holds)."""
    live = subset(live_full)
    pin = pinned()
    changed = [
        f"{method.upper()} {path}"
        for path, item in pin["paths"].items()
        for method, op in item.items()
        if live["paths"].get(path, {}).get(method) != op
    ]
    for section, items in pin["components"].items():
        for key, value in items.items():
            if live["components"].get(section, {}).get(key) != value:
                changed.append(f"components/{section}/{key}")
    return changed


def write_pin(full_bytes: bytes, *, retrieved_at: datetime | None = None) -> dict[str, Any]:
    full = json.loads(full_bytes)
    doc = subset(full)
    CONTRACT_DIR.mkdir(parents=True, exist_ok=True)
    text = json.dumps(doc, indent=1, ensure_ascii=False, sort_keys=False) + "\n"
    SUBSET_PATH.write_text(text, encoding="utf-8", newline="\n")
    record = {
        "source": OPENAPI_URL,
        "retrieved_at": (retrieved_at or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "openapi": full["openapi"],
        "info_version": full["info"]["version"],
        "full_sha256": hashlib.sha256(full_bytes).hexdigest(),
        "full_operations": sum(
            m in {"get", "post", "put", "patch", "delete"}
            for item in full["paths"].values()
            for m in item
        ),
        "pinned_operations": len(OPERATIONS),
        "subset_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }
    PIN_PATH.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n")
    pinned.cache_clear()
    pin_record.cache_clear()
    operation.cache_clear()
    _registry.cache_clear()
    return record


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pin the neatlogs Public API contract subset.")
    sub = parser.add_subparsers(dest="command", required=True)
    pin = sub.add_parser("pin", help="write the subset and PIN.json from a downloaded document")
    pin.add_argument("document", type=Path)
    args = parser.parse_args(argv)
    record = write_pin(args.document.read_bytes())
    print(json.dumps(record, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

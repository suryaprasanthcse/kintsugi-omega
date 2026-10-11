"""Cassettes: record and replay a host's external I/O (spec §5.11 G0/G2, §6.2 step 7).

A cassette holds every exchange a task had with the outside world (the FX API, the document
store, the mock GitHub, the model) **including injected faults at their original position**, so
a replay reproduces the failure deterministically. Exchanges are keyed by

    (service, task, fingerprint(request), occurrence)

where ``occurrence`` counts identical requests within one task. A retry after a 429 is the same
request seen a second time, so it replays the second recorded answer, not the first.

Secrets never enter a cassette: only an allowlist of response headers is kept and request
headers are not recorded at all. The cassette's identity is the SHA-256 of its canonical form
(the ``reproduce`` activity's idempotency key is ``(incident, cassette_sha)``, spec §5.3.2).
"""

from __future__ import annotations

import json
import threading
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from kintsugi.proof.canonical import canonicalize, digest, sha256_hex
from kintsugi.sandbox.httpserve import HttpRequest, HttpResponse

FORMAT = "kintsugi.cassette/v1"
KEPT_RESPONSE_HEADERS = frozenset({"content-type", "retry-after", "x-kintsugi-chaos"})


class CassetteMiss(LookupError):
    """A replayed task made a request the cassette never saw (or saw fewer times)."""


def _body_value(body: bytes) -> Any:
    """JSON bodies are stored parsed (stable under key order and whitespace); others as text."""
    if not body:
        return None
    text = body.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


def request_record(request: HttpRequest, *, path: str | None = None) -> dict[str, Any]:
    return {
        "method": request.method,
        "path": path if path is not None else request.path,
        "query": dict(sorted(request.query.items())),
        "body": _body_value(request.body),
    }


def fingerprint(record: Mapping[str, Any]) -> str:
    return sha256_hex(canonicalize(dict(record)))


def response_record(response: HttpResponse) -> dict[str, Any]:
    headers = {k.lower(): v for k, v in response.headers.items()}
    return {
        "status": response.status,
        "headers": {k: v for k, v in sorted(headers.items()) if k in KEPT_RESPONSE_HEADERS},
        "body": _body_value(response.body),
    }


def to_response(record: Mapping[str, Any]) -> HttpResponse:
    body = record.get("body")
    if body is None:
        raw = b""
    elif isinstance(body, str):
        raw = body.encode("utf-8")
    else:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    return HttpResponse(int(record["status"]), dict(record.get("headers") or {}), raw)


@dataclass(frozen=True)
class Exchange:
    service: str
    task: str
    key: str
    occurrence: int
    request: dict[str, Any]
    response: dict[str, Any]
    latency_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "service": self.service, "task": self.task, "key": self.key,
            "occurrence": self.occurrence, "request": self.request,
            "response": self.response, "latency_ms": self.latency_ms,
        }  # fmt: skip


@dataclass
class Cassette:
    name: str
    exchanges: list[Exchange] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "format": FORMAT,
            "name": self.name,
            "meta": self.meta,
            "exchanges": [e.as_dict() for e in self.exchanges],
        }

    @property
    def sha256(self) -> str:
        return digest(self.as_dict())

    def tasks(self) -> list[str]:
        return sorted({e.task for e in self.exchanges})

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self.as_dict()
        if path.suffix in {".yaml", ".yml"}:
            text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
        else:
            text = json.dumps(data, ensure_ascii=False, indent=1) + "\n"
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    @classmethod
    def load(cls, path: Path) -> Cassette:
        text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(text) if path.suffix in {".yaml", ".yml"} else json.loads(text)
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Cassette:
        if data.get("format") != FORMAT:
            raise ValueError(f"not a {FORMAT} cassette: {data.get('format')!r}")
        exchanges = [
            Exchange(
                service=e["service"], task=e["task"], key=e["key"],
                occurrence=int(e["occurrence"]), request=e["request"],
                response=e["response"], latency_ms=int(e.get("latency_ms", 0)),
            )
            for e in data["exchanges"]
        ]  # fmt: skip
        return cls(name=data["name"], exchanges=exchanges, meta=dict(data.get("meta") or {}))


class Recorder:
    """Thread-safe: concurrent tasks append exchanges; occurrence is counted per task."""

    def __init__(self, name: str, meta: Mapping[str, Any] | None = None) -> None:
        self._cassette = Cassette(name, meta=dict(meta or {}))
        self._seen: Counter[tuple[str, str, str]] = Counter()
        self._lock = threading.Lock()

    def record(
        self, service: str, task: str, request: dict[str, Any], response: dict[str, Any],
        latency_ms: int = 0,
    ) -> Exchange:  # fmt: skip
        key = fingerprint(request)
        with self._lock:
            occurrence = self._seen[(service, task, key)]
            self._seen[(service, task, key)] += 1
            exchange = Exchange(service, task, key, occurrence, request, response, latency_ms)
            self._cassette.exchanges.append(exchange)
        return exchange

    def occurrence(self, service: str, task: str, key: str) -> int:
        """How many times this request has been recorded for the task so far."""
        with self._lock:
            return self._seen[(service, task, key)]

    def cassette(self) -> Cassette:
        with self._lock:
            ordered = sorted(
                self._cassette.exchanges, key=lambda e: (e.task, e.service, e.key, e.occurrence)
            )
            return Cassette(self._cassette.name, list(ordered), dict(self._cassette.meta))


class Player:
    """Serves recorded responses in occurrence order; a miss is an error, never a live call."""

    def __init__(self, cassette: Cassette, *, task_map: Mapping[str, str] | None = None) -> None:
        self._index = {(e.service, e.task, e.key, e.occurrence): e for e in cassette.exchanges}
        self._next: Counter[tuple[str, str, str]] = Counter()
        self._task_map = dict(task_map or {})  # replay task id → recorded task id
        self._lock = threading.Lock()
        self.cassette = cassette

    def play(self, service: str, task: str, request: dict[str, Any]) -> Exchange:
        recorded_task = self._task_map.get(task, task)
        key = fingerprint(request)
        with self._lock:
            n = self._next[(service, recorded_task, key)]
            exchange = self._index.get((service, recorded_task, key, n))
            if exchange is None:
                raise CassetteMiss(
                    f"{service} {request['method']} {request['path']} (task {recorded_task!r}, "
                    f"occurrence {n}) is not in cassette {self.cassette.name!r}"
                )
            self._next[(service, recorded_task, key)] += 1
        return exchange

    def unplayed(self) -> list[Exchange]:
        with self._lock:
            return [e for (s, t, k, n), e in self._index.items() if n >= self._next[(s, t, k)]]

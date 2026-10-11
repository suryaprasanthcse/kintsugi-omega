"""The LLM metering proxy (spec §5.10, ruling D7, invariant I4).

The proxy runs **outside** every sandbox and is the only process that holds a model credential.
A host reaches it at ``/h/{host}/v1/messages`` with a per-host proxy token (never a real key):
the Anthropic SDK in the host is configured with ``ANTHROPIC_BASE_URL=<proxy>/h/<host>`` and
``ANTHROPIC_API_KEY=<proxy token>``. For each call the proxy

1. refuses models it cannot price (it must meter everything it forwards);
2. **reserves** a worst-case cost against the hard daily cap (all accounts) and the account's
   monthly budget, atomically, so concurrent calls can never jointly overshoot a cap; a refusal
   is HTTP 402 ``budget_exceeded``, which the SDK does not retry;
3. forwards to the upstream with the injected credential, relaying server-sent events as they
   arrive and reading ``usage`` from ``message_start``/``message_delta``;
4. **settles** the reservation to the actual cost from ``usage`` and records it per account
   (the actuals pipeline and audit C10 read these rows).

Upstreams: the Anthropic API (billable), an offline stand-in per host (amendment A-23; metered
at list price but not billable, so it never consumes the cap), and cassette replay (G0/G2).
"""

from __future__ import annotations

import hmac
import json
import math
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from kintsugi.econ.governor import BudgetExceeded
from kintsugi.econ.prices import UnpricedModel, price_of, usage_cost, worst_case_cost
from kintsugi.proof.canonical import canonicalize, sha256_hex
from kintsugi.sandbox.cassette import Player, Recorder, request_record
from kintsugi.sandbox.httpserve import HttpRequest, HttpResponse

COST_HEADER = "x-kintsugi-cost-usd"
ANTHROPIC_VERSION = "2023-06-01"


def _error(status: int, kind: str, message: str) -> HttpResponse:
    return HttpResponse.of_json(
        status, {"type": "error", "error": {"type": kind, "message": message}}
    )


# ---- spend ledger -----------------------------------------------------------------------------


@dataclass(frozen=True)
class AccountLimits:
    monthly_usd: float
    daily_usd: float | None = None


def _utcnow() -> datetime:
    return datetime.now(UTC)


class SpendLedger:
    """Reservations and settled charges in SQLite (WAL), shared by every proxy process."""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS reservations (
        id TEXT PRIMARY KEY, account TEXT NOT NULL, day TEXT NOT NULL, month TEXT NOT NULL,
        amount_usd REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS charges (
        id TEXT PRIMARY KEY, account TEXT NOT NULL, day TEXT NOT NULL, month TEXT NOT NULL,
        model TEXT NOT NULL, upstream TEXT NOT NULL, billable INTEGER NOT NULL,
        cost_usd REAL NOT NULL, usage TEXT NOT NULL, task TEXT NOT NULL, ts TEXT NOT NULL);
    """

    def __init__(
        self,
        path: Path | str,
        *,
        daily_cap_usd: float,
        limits: Mapping[str, AccountLimits],
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        if daily_cap_usd <= 0:
            raise ValueError("the daily cap must be positive")
        self.daily_cap_usd = daily_cap_usd
        self.limits = dict(limits)
        self._clock = clock
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None,
                                   timeout=30.0)  # fmt: skip
        if str(path) != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(self._SCHEMA)

    def _period(self) -> tuple[str, str, str]:
        now = self._clock()
        return now.strftime("%Y-%m-%d"), now.strftime("%Y-%m"), now.isoformat()

    def _committed(self, where: str, args: tuple[Any, ...]) -> float:
        charged = self._db.execute(
            f"SELECT COALESCE(SUM(cost_usd), 0) FROM charges WHERE billable = 1 AND {where}", args
        ).fetchone()[0]
        reserved = self._db.execute(
            f"SELECT COALESCE(SUM(amount_usd), 0) FROM reservations WHERE {where}", args
        ).fetchone()[0]
        return float(charged) + float(reserved)

    def reserve(self, account: str, amount_usd: float) -> str:
        """Hold ``amount_usd`` against every cap, or refuse it whole (never a partial overshoot)."""
        if account not in self.limits:
            raise BudgetExceeded(f"{account}: no budget is configured for this account")
        day, month, _ = self._period()
        limit = self.limits[account]
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                checks = [
                    ("daily cap", self.daily_cap_usd, self._committed("day = ?", (day,))),
                    (f"{account} monthly budget", limit.monthly_usd,
                     self._committed("account = ? AND month = ?", (account, month))),
                ]  # fmt: skip
                if limit.daily_usd is not None:
                    today = self._committed("account = ? AND day = ?", (account, day))
                    checks.append((f"{account} daily budget", limit.daily_usd, today))
                for name, cap, committed in checks:
                    if committed + amount_usd > cap + 1e-12:
                        raise BudgetExceeded(
                            f"{day}: reserving ${amount_usd:.4f} would exceed the {name} of "
                            f"${cap:.2f} (committed ${committed:.4f})"
                        )
                rid = uuid.uuid4().hex
                self._db.execute("INSERT INTO reservations VALUES (?, ?, ?, ?, ?)",
                                 (rid, account, day, month, amount_usd))  # fmt: skip
                self._db.execute("COMMIT")
                return rid
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def release(self, rid: str | None) -> None:
        if rid is None:
            return
        with self._lock:
            self._db.execute("DELETE FROM reservations WHERE id = ?", (rid,))

    def settle(
        self, rid: str | None, *, account: str, model: str, cost_usd: float,
        usage: Mapping[str, Any], upstream: str, billable: bool, task: str = "",
    ) -> str:  # fmt: skip
        """Replace the reservation (if any) with the actual charge."""
        day, month, ts = self._period()
        charge_id = rid or uuid.uuid4().hex
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                if rid is not None:
                    self._db.execute("DELETE FROM reservations WHERE id = ?", (rid,))
                self._db.execute(
                    "INSERT INTO charges VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (charge_id, account, day, month, model, upstream, int(billable), cost_usd,
                     json.dumps(dict(usage), sort_keys=True), task, ts),
                )  # fmt: skip
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return charge_id

    def spent(
        self, *, account: str | None = None, day: str | None = None, billable: bool | None = True
    ) -> float:
        clauses, args = ["1 = 1"], []
        if account is not None:
            clauses.append("account = ?")
            args.append(account)
        if day is not None:
            clauses.append("day = ?")
            args.append(day)
        if billable is not None:
            clauses.append("billable = ?")
            args.append(int(billable))
        with self._lock:
            row = self._db.execute(
                f"SELECT COALESCE(SUM(cost_usd), 0) FROM charges WHERE {' AND '.join(clauses)}",
                tuple(args),
            ).fetchone()
        return float(row[0])

    def charges(self, account: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM charges" + (" WHERE account = ?" if account else "") + " ORDER BY ts"
        with self._lock:
            cursor = self._db.execute(sql, (account,) if account else ())
            names = [c[0] for c in cursor.description]
            return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    def outstanding_reservations(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0])

    def close(self) -> None:
        self._db.close()


# ---- server-sent events -----------------------------------------------------------------------


class SseUsage:
    """Accumulates ``usage`` from a Messages stream (message_start, then message_delta)."""

    def __init__(self) -> None:
        self.usage: dict[str, Any] = {}
        self._buffer = b""

    def feed(self, chunk: bytes) -> None:
        self._buffer += chunk.replace(b"\r\n", b"\n")
        while b"\n\n" in self._buffer:
            event, self._buffer = self._buffer.split(b"\n\n", 1)
            data = b"".join(
                line[5:].strip() for line in event.split(b"\n") if line.startswith(b"data:")
            )
            if not data:
                continue
            try:
                payload = json.loads(data)
            except ValueError:
                continue
            if payload.get("type") == "message_start":
                self.usage.update((payload.get("message") or {}).get("usage") or {})
            elif payload.get("type") == "message_delta":
                delta = {k: v for k, v in (payload.get("usage") or {}).items() if v is not None}
                self.usage.update(delta)


def _sse(event: str, payload: Mapping[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


def sse_from_message(message: Mapping[str, Any]) -> Iterator[bytes]:
    """Render a complete message as a Messages API event stream (for offline and replay)."""
    usage = dict(message.get("usage") or {})
    opening_usage = {**usage, "output_tokens": min(1, int(usage.get("output_tokens", 0)))}
    start = {**message, "content": [], "stop_reason": None, "usage": opening_usage}
    yield _sse("message_start", {"type": "message_start", "message": start})
    for index, block in enumerate(message.get("content") or []):
        if block.get("type") == "tool_use":
            opening, delta = (
                {**block, "input": {}},
                {"type": "input_json_delta", "partial_json": json.dumps(block.get("input", {}))},
            )
        elif block.get("type") == "text":
            opening, delta = (
                {"type": "text", "text": ""},
                {"type": "text_delta", "text": block.get("text", "")},
            )
        else:
            opening, delta = block, None
        started = {"type": "content_block_start", "index": index, "content_block": opening}
        yield _sse("content_block_start", started)
        if delta is not None:
            yield _sse("content_block_delta",
                       {"type": "content_block_delta", "index": index, "delta": delta})  # fmt: skip
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": index})
    yield _sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": message.get("stop_reason"),
                  "stop_sequence": message.get("stop_sequence")},
        "usage": {"output_tokens": int(usage.get("output_tokens", 0))},
    })  # fmt: skip
    yield _sse("message_stop", {"type": "message_stop"})


# ---- upstreams --------------------------------------------------------------------------------


@dataclass
class Reply:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    message: dict[str, Any] | None = None  # a complete (non-streaming) message
    events: Iterator[bytes] | None = None  # a relayed event stream
    error: bytes = b""


class Upstream(Protocol):
    name: str
    billable: bool

    def messages(self, host: str, body: dict[str, Any], request: HttpRequest) -> Reply: ...

    def count_tokens(self, host: str, body: dict[str, Any], request: HttpRequest) -> Reply: ...


def estimate_tokens(value: Any) -> int:
    """A deterministic size estimate (≈4 bytes per token) for upstreams that report no usage."""
    return max(1, math.ceil(len(canonicalize(value)) / 4))


def _prompt_view(body: Mapping[str, Any]) -> dict[str, Any]:
    return {k: body[k] for k in ("system", "messages", "tools") if k in body}


def complete_message(body: Mapping[str, Any], partial: Mapping[str, Any]) -> dict[str, Any]:
    """Fill in id, model, role and estimated usage around a responder's content."""
    content = list(partial.get("content") or [])
    return {
        "id": "msg_offline_" + sha256_hex(canonicalize(dict(body)))[:24],
        "type": "message",
        "role": "assistant",
        "model": body.get("model"),
        "content": content,
        "stop_reason": partial.get("stop_reason", "end_turn"),
        "stop_sequence": None,
        "usage": {"input_tokens": estimate_tokens(_prompt_view(body)),
                  "output_tokens": estimate_tokens(content),
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    }  # fmt: skip


Responder = Callable[[dict[str, Any]], dict[str, Any]]


class OfflineUpstream:
    """Deterministic per-host stand-ins (amendment A-23). Metered at list price, never billable."""

    name = "offline"
    billable = False

    def __init__(self, responders: Mapping[str, Responder]) -> None:
        self.responders = dict(responders)

    def messages(self, host: str, body: dict[str, Any], request: HttpRequest) -> Reply:
        responder = self.responders.get(host)
        if responder is None:
            return Reply(501, error=_error(501, "not_implemented",
                                           f"no offline model for host {host!r}").body)  # fmt: skip
        message = complete_message(body, responder(body))
        if body.get("stream"):
            return Reply(200, {"content-type": "text/event-stream"},
                         events=sse_from_message(message))  # fmt: skip
        return Reply(200, {}, message=message)

    def count_tokens(self, host: str, body: dict[str, Any], request: HttpRequest) -> Reply:
        return Reply(200, {}, message={"input_tokens": estimate_tokens(_prompt_view(body))})


class CassetteUpstream:
    """Replays recorded model answers (G0/G2): deterministic, never billable."""

    name = "cassette"
    billable = False

    def __init__(self, player: Player) -> None:
        self.player = player

    def messages(self, host: str, body: dict[str, Any], request: HttpRequest) -> Reply:
        record = request_record(request, path="/v1/messages")
        message = self.player.play(f"llm:{host}", request.task, record).response["body"]
        if body.get("stream"):
            return Reply(200, {"content-type": "text/event-stream"},
                         events=sse_from_message(message))  # fmt: skip
        return Reply(200, {}, message=message)

    def count_tokens(self, host: str, body: dict[str, Any], request: HttpRequest) -> Reply:
        return Reply(200, {}, message={"input_tokens": estimate_tokens(_prompt_view(body))})


class AnthropicUpstream:
    """The Anthropic API. The key is injected here and never forwarded from the client."""

    name = "anthropic"
    billable = True

    def __init__(
        self, api_key: str, *, base_url: str = "https://api.anthropic.com", timeout_s: float = 600
    ) -> None:
        self._key = api_key
        self._client = httpx.Client(base_url=base_url, timeout=timeout_s)

    def __repr__(self) -> str:
        return "AnthropicUpstream(<key withheld>)"

    def _headers(self, request: HttpRequest) -> dict[str, str]:
        headers = {
            "x-api-key": self._key,
            "anthropic-version": request.headers.get("anthropic-version", ANTHROPIC_VERSION),
            "content-type": "application/json",
        }
        if "anthropic-beta" in request.headers:
            headers["anthropic-beta"] = request.headers["anthropic-beta"]
        return headers

    def _send(self, path: str, body: dict[str, Any], request: HttpRequest) -> Reply:
        built = self._client.build_request("POST", path, headers=self._headers(request),
                                           content=json.dumps(body).encode("utf-8"))  # fmt: skip
        streaming = bool(body.get("stream"))
        response = self._client.send(built, stream=streaming)
        kept = {k: v for k, v in response.headers.items()
                if k.lower() in {"content-type", "request-id", "retry-after"}}  # fmt: skip
        if response.status_code >= 400:
            error = response.read()
            response.close()
            return Reply(response.status_code, kept, error=error)
        if streaming:

            def relay() -> Iterator[bytes]:
                try:
                    yield from response.iter_bytes()
                finally:
                    response.close()

            return Reply(200, kept, events=relay())
        return Reply(200, kept, message=response.json())

    def messages(self, host: str, body: dict[str, Any], request: HttpRequest) -> Reply:
        return self._send("/v1/messages", body, request)

    def count_tokens(self, host: str, body: dict[str, Any], request: HttpRequest) -> Reply:
        return self._send("/v1/messages/count_tokens", body, request)


# ---- the proxy --------------------------------------------------------------------------------


def account_of(host: str) -> str:
    return f"host:{host}"


class LlmProxy:
    def __init__(
        self,
        upstream: Upstream,
        ledger: SpendLedger,
        tokens: Mapping[str, str],
        *,
        recorder: Recorder | None = None,
    ) -> None:
        self.upstream = upstream
        self.ledger = ledger
        self.tokens = dict(tokens)  # host → proxy token
        self.recorder = recorder

    def __call__(self, request: HttpRequest) -> HttpResponse:
        parts = request.path.strip("/").split("/")
        if len(parts) < 4 or parts[0] != "h" or parts[2:4] != ["v1", "messages"]:
            return _error(404, "not_found_error", f"no route {request.path}")
        host, count = parts[1], parts[4:] == ["count_tokens"]
        if len(parts) > 4 and not count:
            return _error(404, "not_found_error", f"no route {request.path}")
        expected = self.tokens.get(host)
        offered = request.headers.get("x-api-key") or request.headers.get(
            "authorization", ""
        ).removeprefix("Bearer ")
        if expected is None or not hmac.compare_digest(offered, expected):
            return _error(401, "authentication_error", f"bad proxy token for host {host!r}")
        if request.method != "POST":
            return _error(405, "invalid_request_error", "POST only")
        try:
            body = request.json()
            model = str(body["model"])
            price_of(model)
        except UnpricedModel as unpriced:  # before KeyError, its base class
            return _error(400, "invalid_request_error", f"model {unpriced} is not priced")
        except (ValueError, KeyError, TypeError):
            return _error(400, "invalid_request_error", "a JSON body with a model is required")
        if count:
            return self._reply_plain(self.upstream.count_tokens(host, body, request))
        return self._messages(host, model, body, request)

    @staticmethod
    def _reply_plain(reply: Reply) -> HttpResponse:
        if reply.status >= 400:
            return HttpResponse(reply.status, {"content-type": "application/json"}, reply.error)
        return HttpResponse.of_json(reply.status, reply.message, reply.headers)

    def _messages(
        self, host: str, model: str, body: dict[str, Any], request: HttpRequest
    ) -> HttpResponse:
        account = account_of(host)
        rid: str | None = None
        if self.upstream.billable:
            bound = worst_case_cost(model, len(request.body), int(body.get("max_tokens", 4096)))
            try:
                rid = self.ledger.reserve(account, bound)
            except BudgetExceeded as refused:
                return _error(402, "budget_exceeded", str(refused))
        try:
            reply = self.upstream.messages(host, body, request)
        except Exception as failure:
            self.ledger.release(rid)
            return _error(502, "upstream_error", f"{self.upstream.name}: {failure}")
        if reply.status >= 400:
            self.ledger.release(rid)
            return HttpResponse(reply.status, {"content-type": "application/json", **reply.headers},
                                reply.error)  # fmt: skip
        settle = {"account": account, "model": model, "upstream": self.upstream.name,
                  "billable": self.upstream.billable, "task": request.task}  # fmt: skip
        if reply.events is not None:
            return HttpResponse(200, {"content-type": "text/event-stream", **reply.headers},
                                stream=self._metered(reply.events, rid, settle))  # fmt: skip
        assert reply.message is not None
        usage = reply.message.get("usage") or {}
        cost = usage_cost(model, usage)
        self.ledger.settle(rid, cost_usd=cost, usage=usage, **settle)
        if self.recorder is not None:
            self.recorder.record(f"llm:{host}", request.task,
                                 request_record(request, path="/v1/messages"),
                                 {"status": 200, "headers": {}, "body": reply.message})  # fmt: skip
        headers = {**reply.headers, COST_HEADER: f"{cost:.8f}"}
        return HttpResponse.of_json(200, reply.message, headers)

    def _metered(
        self, events: Iterator[bytes], rid: str | None, settle: dict[str, Any]
    ) -> Iterator[bytes]:
        meter = SseUsage()
        try:
            for chunk in events:
                meter.feed(chunk)
                yield chunk
        finally:  # also on a client disconnect: what was generated is charged
            cost = usage_cost(settle["model"], meter.usage)
            self.ledger.settle(rid, cost_usd=cost, usage=meter.usage, **settle)

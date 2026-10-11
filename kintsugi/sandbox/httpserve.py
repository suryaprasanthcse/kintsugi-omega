"""A minimal threaded HTTP server for the sandbox fabric (proxies and mock services).

Every proxy in front of a sandboxed host (LLM metering, chaos, egress) is one of these, so the
request/response shape is shared and each component stays a plain function that tests can call
without sockets. Responses may stream (the LLM proxy relays server-sent events).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Any
from urllib.parse import parse_qsl, urlsplit

# Correlates a host's external calls with its task (chaos decisions, cassette keys).
TASK_HEADER = "x-kintsugi-task"


@dataclass(frozen=True)
class HttpRequest:
    method: str
    path: str
    query: Mapping[str, str]
    headers: Mapping[str, str]  # lower-cased names
    body: bytes = b""

    @property
    def task(self) -> str:
        return self.headers.get(TASK_HEADER, "")

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8")) if self.body else None


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    stream: Iterable[bytes] | None = None  # sent chunk by chunk when set; ``body`` is ignored

    @classmethod
    def of_json(
        cls, status: int, value: Any, headers: Mapping[str, str] | None = None
    ) -> HttpResponse:
        merged = {"content-type": "application/json", **(headers or {})}
        return cls(status, merged, json.dumps(value, ensure_ascii=False).encode("utf-8"))

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8")) if self.body else None


Handler = Callable[[HttpRequest], HttpResponse]

_HOP_BY_HOP = frozenset(
    {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade",
     "proxy-authorization", "proxy-authenticate", "content-length", "host"}
)  # fmt: skip


def forwardable(headers: Mapping[str, str]) -> dict[str, str]:
    """Drop hop-by-hop headers before relaying a request or response."""
    return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP}


def _make_handler_class(handler: Handler) -> type[BaseHTTPRequestHandler]:
    class _Adapter(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _dispatch(self) -> None:
            split = urlsplit(self.path)
            length = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(length) if length else b""
            request = HttpRequest(
                method=self.command,
                path=split.path,
                query=dict(parse_qsl(split.query, keep_blank_values=True)),
                headers={k.lower(): v for k, v in self.headers.items()},
                body=body,
            )
            try:
                response = handler(request)
            except Exception as error:  # a handler bug must not hang the client
                response = HttpResponse.of_json(
                    500, {"type": "error", "error": {"type": "proxy_error", "message": str(error)}}
                )
            self._send(response)

        def _send(self, response: HttpResponse) -> None:
            self.send_response(response.status)
            for name, value in forwardable(response.headers).items():
                self.send_header(name, value)
            if response.stream is None:
                self.send_header("content-length", str(len(response.body)))
                self.end_headers()
                self.wfile.write(response.body)
                return
            self.send_header("transfer-encoding", "chunked")
            self.end_headers()
            for chunk in response.stream:
                if chunk:
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                    self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _dispatch

        def log_message(self, format: str, *args: Any) -> None:
            return  # quiet: the proxies keep their own structured records

    return _Adapter


class Server:
    """Serve ``handler`` on ``host:port`` (0 = ephemeral) in a daemon thread."""

    def __init__(self, handler: Handler, *, host: str = "127.0.0.1", port: int = 0) -> None:
        self._httpd = ThreadingHTTPServer((host, port), _make_handler_class(handler))
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host!s}:{port}"

    def start(self) -> Server:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def wait(self) -> None:
        """Block until the server stops (for servers run as a process's main job)."""
        self._thread.join()

    def __enter__(self) -> Server:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

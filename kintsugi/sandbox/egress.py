"""The egress gateway: the only door out of a sandbox network (spec §5.10 "Egress").

Sandboxed hosts sit on an ``--internal`` Docker network with no route anywhere. This gateway
is the one container attached to both that network and the outside; it forwards
``/{route}/{path}`` to an allowlisted upstream (the LLM proxy, the chaos proxy, the neatlogs
ingest) and answers 403 ``egress_denied`` for everything else. A route may inject a credential
from the gateway's own environment (e.g. the neatlogs key), so the host only ever holds a
``kintsugi-proxy-`` placeholder: no secrets inside sandboxes.

Runs with the standard library only (it is started with ``python -m kintsugi.sandbox.egress``
inside a stock ``python:3.12-slim`` container with the package mounted read-only).
"""

from __future__ import annotations

import argparse
import http.client
import os
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlsplit

from kintsugi.sandbox.httpserve import Handler, HttpRequest, HttpResponse, Server, forwardable


@dataclass(frozen=True)
class Route:
    upstream: str  # http://host:port[/base]
    inject: Mapping[str, str] = field(default_factory=dict)  # header → value (from env)


def _deny(reason: str) -> HttpResponse:
    error = {"type": "egress_denied", "message": reason}
    return HttpResponse.of_json(403, {"type": "error", "error": error})


def _relay(
    response: http.client.HTTPResponse, connection: http.client.HTTPConnection
) -> Iterator[bytes]:
    try:
        while chunk := response.read1(65536):
            yield chunk
    finally:
        connection.close()


def gateway(routes: Mapping[str, Route], *, timeout_s: float = 600.0) -> Handler:
    def handle(request: HttpRequest) -> HttpResponse:
        name, _, rest = request.path.lstrip("/").partition("/")
        route = routes.get(name)
        if route is None:
            return _deny(f"route {name!r} is not on the egress allowlist")
        target = urlsplit(route.upstream)
        if target.scheme != "http" or target.hostname is None:
            return _deny(f"route {name!r} has an unusable upstream")
        path = target.path.rstrip("/") + "/" + rest
        if request.query:
            path += "?" + urlencode(dict(request.query))
        headers = forwardable(request.headers)
        for header in route.inject:
            headers.pop(header.lower(), None)
        headers.update(route.inject)
        port = target.port or 80
        connection = http.client.HTTPConnection(target.hostname, port, timeout=timeout_s)
        connection.request(request.method, path, body=request.body or None, headers=headers)
        response = connection.getresponse()
        reply_headers = {k.lower(): v for k, v in response.getheaders()}
        return HttpResponse(response.status, reply_headers, stream=_relay(response, connection))

    return handle


def parse_routes(specs: Sequence[str], env: Mapping[str, str]) -> dict[str, Route]:
    """``name=http://host:port`` or ``name=http://host:port,header:ENV_VAR`` (inject header)."""
    routes: dict[str, Route] = {}
    for spec in specs:
        name, _, rest = spec.partition("=")
        upstream, *injections = rest.split(",")
        inject = {}
        for item in injections:
            header, _, variable = item.partition(":")
            if variable not in env:
                raise SystemExit(f"egress: {variable} is not set for route {name}")
            inject[header] = env[variable]
        routes[name] = Route(upstream, inject)
    return routes


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The egress allowlist gateway (spec §5.10).")
    parser.add_argument("--listen", default="0.0.0.0:8080")
    parser.add_argument("--route", action="append", default=[], help="name=http://host:port")
    args = parser.parse_args(argv)
    host, _, port = args.listen.rpartition(":")
    routes = parse_routes(args.route, os.environ)
    print(f"egress gateway on {args.listen}: routes {sorted(routes)}", flush=True)
    server = Server(gateway(routes), host=host, port=int(port)).start()
    try:
        server.wait()
    except KeyboardInterrupt:
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

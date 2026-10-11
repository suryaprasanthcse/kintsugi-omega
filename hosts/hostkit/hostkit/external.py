"""Calls to a host's "external" services, always through the chaos proxy (spec §5.10).

``call("fx", "GET", "/rate", params=...)`` goes to ``{KINTSUGI_EXT_URL}/fx/rate`` with the
current task id. Status codes are returned, not raised: each host's own tools decide what a 429
means (Ledger and Scout raise their own ``RateLimited`` for their own retry wrappers).
"""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from typing import Any

import httpx

from hostkit.context import task_headers

EXT_URL_ENV = "KINTSUGI_EXT_URL"
_local = threading.local()


def base_url() -> str:
    url = os.environ.get(EXT_URL_ENV)
    if not url:
        raise RuntimeError(f"{EXT_URL_ENV} is not set: external calls go through the chaos proxy")
    return url.rstrip("/")


def _client() -> httpx.Client:
    client = getattr(_local, "client", None)
    if client is None:
        client = httpx.Client(timeout=30.0)
        _local.client = client
    return client


def call(
    service: str,
    method: str,
    path: str,
    *,
    params: Mapping[str, str] | None = None,
    json: Any = None,
) -> httpx.Response:
    url = f"{base_url()}/{service}/{path.lstrip('/')}"
    return _client().request(method, url, params=params, json=json, headers=task_headers())

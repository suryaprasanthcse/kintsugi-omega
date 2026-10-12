"""A deterministic geo API: ``GET /region?domain=acme.example`` → ``{"region": "EMEA"}``."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping
from typing import Any

REGIONS = ("AMER-East", "AMER-West", "EMEA", "APAC", "LATAM")
SERVICE_TIME_S = 0.006


def region(domain: str) -> str:
    digest = hashlib.sha256(domain.lower().encode("utf-8")).digest()
    return REGIONS[digest[0] % len(REGIONS)]


def handle(method: str, path: str, query: Mapping[str, str], body: bytes) -> tuple[int, Any]:
    time.sleep(SERVICE_TIME_S)
    if method != "GET" or path != "/region":
        return 404, {"error": "not found"}
    domain = query.get("domain", "")
    if "." not in domain:
        return 400, {"error": f"bad domain {domain!r}"}
    return 200, {"domain": domain, "region": region(domain)}

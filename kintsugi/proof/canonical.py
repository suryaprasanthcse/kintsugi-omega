"""RFC 8785 JSON Canonicalization Scheme (JCS) and the digests built on it.

Spike A15: the ``rfc8785`` library provides JCS. Integers must stay within ±(2^53 − 1) and floats
must be finite, as RFC 8785 requires. The restricted fallback from the spec (sorted keys, no
whitespace; strings, integers, booleans and null only) is kept and tested to agree with the
library on its domain, so a missing library could never silently change a hash.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

import rfc8785

_MAX_SAFE_INTEGER = 2**53 - 1


def canonicalize(value: Any) -> bytes:
    """The RFC 8785 canonical UTF-8 bytes of a JSON-compatible value."""
    return rfc8785.dumps(value)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest(value: Any) -> str:
    """``sha256:<hex>`` of the canonical form."""
    return "sha256:" + sha256_hex(canonicalize(value))


def restricted_canonicalize(value: Any) -> bytes:
    """Spec A15 fallback: JCS restricted to strings, integers, booleans, null, lists, objects."""
    return _encode(value).encode("utf-8")


def _encode(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        if abs(value) > _MAX_SAFE_INTEGER:
            raise ValueError(f"integer outside the I-JSON safe range: {value}")
        return str(value)
    if isinstance(value, str):
        # Python's escaping equals JCS: \b \f \n \r \t, \u00xx (lowercase) for other controls,
        # \" and \\; everything else, '/' and non-ASCII included, is emitted verbatim.
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, Mapping):
        keys = list(value.keys())
        if not all(isinstance(key, str) for key in keys):
            raise TypeError("object keys must be strings")
        ordered = sorted(keys, key=lambda key: key.encode("utf-16-be"))  # UTF-16 code units
        members = (f"{_encode(key)}:{_encode(value[key])}" for key in ordered)
        return "{" + ",".join(members) + "}"
    if isinstance(value, Sequence):
        return "[" + ",".join(_encode(item) for item in value) + "]"
    raise TypeError(f"not in the restricted canonical domain: {type(value).__name__}")

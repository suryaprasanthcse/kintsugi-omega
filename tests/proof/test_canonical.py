from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest
import rfc8785
from hypothesis import given
from hypothesis import strategies as st

from kintsugi.proof.canonical import canonicalize, digest, restricted_canonicalize


def test_rfc8785_section_3_2_2_example() -> None:
    # RFC 8785 §3.2.2: number serialisation, string escaping, literals, key order.
    source = (
        '{"numbers": [333333333.33333329, 1E30, 4.50, 2e-3, 0.000000000000000000000000001],'
        ' "string": "\\u20ac$\\u000F\\u000aA\'\\u0042\\u0022\\u005c\\\\\\"\\/",'
        ' "literals": [null, true, false]}'
    )
    expected = (
        '{"literals":[null,true,false],'
        '"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],'
        '"string":"€$\\u000f\\nA\'B\\"\\\\\\\\\\"/"}'
    )
    assert canonicalize(json.loads(source)) == expected.encode("utf-8")


def test_rfc8785_section_3_2_3_key_order_is_utf16_code_units() -> None:
    # Escapes, not literals: editors may decompose U+FB33 into U+05D3 U+05BC.
    value = {
        "€": "Euro Sign",
        "\r": "Carriage Return",
        "דּ": "Hebrew Letter Dalet With Dagesh",
        "1": "One",
        "\U0001f600": "Emoji: Grinning Face",
        "\u0080": "Control",
        "ö": "Latin Small Letter O With Diaeresis",
    }
    keys = list(json.loads(canonicalize(value)).keys())
    # U+1F600 is the surrogate pair D83D DE00, so it sorts before U+FB33 in UTF-16.
    assert keys == ["\r", "1", "\u0080", "ö", "€", "\U0001f600", "דּ"]


def test_digest_is_sha256_of_canonical_bytes() -> None:
    value = {"b": [1, 2], "a": "x"}
    expected = hashlib.sha256(b'{"a":"x","b":[1,2]}').hexdigest()
    assert digest(value) == f"sha256:{expected}"
    assert digest({"a": "x", "b": [1, 2]}) == digest(value)  # insertion order is irrelevant


def test_jcs_rejects_non_finite_numbers() -> None:
    with pytest.raises(rfc8785.CanonicalizationError):
        canonicalize({"x": float("nan")})


_SAFE_INT = st.integers(min_value=-(2**53 - 1), max_value=2**53 - 1)
_RESTRICTED: st.SearchStrategy[Any] = st.recursive(
    st.none() | st.booleans() | _SAFE_INT | st.text(),
    lambda children: (
        st.lists(children, max_size=5) | st.dictionaries(st.text(max_size=8), children, max_size=5)
    ),
    max_leaves=25,
)


@given(_RESTRICTED)
def test_restricted_fallback_agrees_with_the_library(value: Any) -> None:
    # Spike A15: the spec's fallback must never change a hash on its own domain.
    assert restricted_canonicalize(value) == canonicalize(value)


def test_restricted_fallback_refuses_floats_and_unsafe_integers() -> None:
    with pytest.raises(TypeError):
        restricted_canonicalize({"x": 1.5})
    with pytest.raises(ValueError, match="safe range"):
        restricted_canonicalize(2**53)

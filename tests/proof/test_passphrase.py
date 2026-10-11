from __future__ import annotations

import re

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kintsugi.proof import passphrase

_WORD = re.compile(r"^[bdfghjklmnprstvz][aiou][bdfghjklmnprstvz][aiou]$")


@given(st.integers(min_value=0, max_value=2**128 - 1))
def test_nonce_encoding_round_trips(value: int) -> None:
    phrase = passphrase.encode(value, bits=128)
    assert passphrase.decode(phrase, bits=128) == value


@given(st.integers(min_value=0, max_value=2**72 - 1))
def test_honeytoken_encoding_round_trips(value: int) -> None:
    assert passphrase.decode(passphrase.encode(value, bits=72), bits=72) == value


def test_fresh_nonces_and_honeytokens_have_the_expected_shape() -> None:
    nonce, token = passphrase.new_nonce(), passphrase.new_honeytoken()
    assert len(nonce.split("-")) == 11
    assert len(token.split("-")) == 6
    for word in nonce.split("-") + token.split("-"):
        assert _WORD.match(word), word
    assert passphrase.is_nonce(nonce)
    assert not passphrase.is_nonce(token)


def test_passphrases_do_not_look_like_secrets() -> None:
    # No digits and no long alphanumeric runs: nothing a hex/base64 secret detector keys on.
    for _ in range(200):
        phrase = passphrase.new_nonce()
        assert not re.search(r"[0-9A-Z]", phrase)
        assert max(len(word) for word in phrase.split("-")) == 4


def test_fresh_nonces_are_unique() -> None:
    assert len({passphrase.new_nonce() for _ in range(2000)}) == 2000


@pytest.mark.parametrize(
    ("phrase", "message"),
    [
        ("bafo-kimu", "expected 11 words"),
        ("-".join(["bafo"] * 10 + ["xyzq"]), "not a passphrase word"),
        ("-".join(["zuzu"] * 11), "more than 128 bits"),
    ],
)
def test_decode_rejects_malformed_phrases(phrase: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        passphrase.decode(phrase, bits=128)


def test_hex_nonces_are_not_nonces() -> None:
    assert not passphrase.is_nonce("9f2c4e1a" * 4)


def test_encode_rejects_out_of_range_values() -> None:
    with pytest.raises(ValueError, match="does not fit"):
        passphrase.encode(2**128, bits=128)
    with pytest.raises(ValueError, match="does not fit"):
        passphrase.encode(-1, bits=128)

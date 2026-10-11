"""Passphrase encoding for attestation nonces and sabotage honeytokens (spike N1).

Entire redacts secret-looking lines in checkpoint transcripts. A 128-bit nonce written in hex
could be blanked, breaking audit check C4 (the nonce must appear in the transcript). A
random-looking honeytoken could be blanked too, which would make check C6's "0 honeytoken hits"
pass vacuously. Both are therefore written as pronounceable word sequences such as
``bafo-kimu-zato-…``. Stage 2 verifies byte for byte that they survive redaction.

Each word is two consonant-vowel syllables: 16 consonants × 4 vowels = 64 syllables (6 bits),
so one word carries 12 bits. The encoding is a bijection on fixed-width integers.
"""

from __future__ import annotations

import secrets

_CONSONANTS = "bdfghjklmnprstvz"
_VOWELS = "aiou"
_SYLLABLES = tuple(c + v for c in _CONSONANTS for v in _VOWELS)
_SYLLABLE_INDEX = {syllable: i for i, syllable in enumerate(_SYLLABLES)}
WORD_BITS = 12
_WORD_RADIX = 1 << WORD_BITS

NONCE_BITS = 128
HONEYTOKEN_BITS = 72


def words_for(bits: int) -> int:
    return -(-bits // WORD_BITS)


def encode(value: int, *, bits: int) -> str:
    """Encode a non-negative integer of at most ``bits`` bits as hyphen-joined words."""
    if bits <= 0 or value < 0 or value >= 1 << bits:
        raise ValueError(f"value does not fit in {bits} bits")
    digits: list[int] = []
    for _ in range(words_for(bits)):
        value, digit = divmod(value, _WORD_RADIX)
        digits.append(digit)
    return "-".join(_word(digit) for digit in reversed(digits))


def decode(phrase: str, *, bits: int) -> int:
    """Inverse of :func:`encode`."""
    words = phrase.split("-")
    if len(words) != words_for(bits):
        raise ValueError(f"expected {words_for(bits)} words, got {len(words)}")
    value = 0
    for word in words:
        value = value * _WORD_RADIX + _digit(word)
    if value >= 1 << bits:
        raise ValueError(f"phrase encodes more than {bits} bits")
    return value


def new_nonce() -> str:
    """A fresh 128-bit attestation nonce (11 words)."""
    return encode(secrets.randbits(NONCE_BITS), bits=NONCE_BITS)


def new_honeytoken() -> str:
    """A fresh 72-bit sabotage honeytoken (6 words)."""
    return encode(secrets.randbits(HONEYTOKEN_BITS), bits=HONEYTOKEN_BITS)


def is_nonce(text: str) -> bool:
    try:
        decode(text, bits=NONCE_BITS)
    except ValueError:
        return False
    return True


def _word(digit: int) -> str:
    return _SYLLABLES[digit >> 6] + _SYLLABLES[digit & 0x3F]


def _digit(word: str) -> int:
    if len(word) != 4 or word[:2] not in _SYLLABLE_INDEX or word[2:] not in _SYLLABLE_INDEX:
        raise ValueError(f"not a passphrase word: {word!r}")
    return (_SYLLABLE_INDEX[word[:2]] << 6) | _SYLLABLE_INDEX[word[2:]]

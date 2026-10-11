"""The trace tag schema and the optimistic-concurrency tag writer (spec §4.1.3).

``PATCH /traces/{id}`` replaces a trace's tags (at most 10, each 1–128 characters) and requires
``expectedRevision``. Omega writes read-modify-write: read the trace and its
``metadataRevision``, merge, patch; on a revision conflict read again and retry. The idempotency
key is derived from (trace, revision, tags), so a retry after a lost response is the same
logical write and a write against a newer revision is a new one.

Single-valued keys (``host``, ``inc``, ``phase``, ``split``, ``seed``, ``run``) replace any
previous value; ``hyp``, ``ab`` and ``gate`` may repeat. Tags Omega does not recognise are kept
as they are: a trace's other owners may tag it too. A merge that would exceed 10 tags is refused
rather than silently dropping evidence.
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

MAX_TAGS = 10
MAX_TAG_LENGTH = 128
SINGLE_VALUED = frozenset({"host", "inc", "phase", "split", "seed", "run"})
PHASES = frozenset(
    {"baseline", "repro", "before", "after", "canary", "vacc", "sabotage", "selfrepair"}
)
_RULES: dict[str, re.Pattern[str]] = {
    "host": re.compile(r"[a-z][a-z0-9-]{0,62}"),
    "inc": re.compile(r"(?:INC|VAC)-[A-Za-z0-9-]{1,40}"),
    "phase": re.compile("|".join(sorted(PHASES))),
    "hyp": re.compile(r"H[1-9][0-9]?"),
    "ab": re.compile(r"AB-[0-9]+@[0-9]+\.[0-9]+"),
    # The spec names train and hold; the fleet's eval runner writes holdout and live-<n>.
    "split": re.compile(r"train|hold|holdout|live-[0-9]+"),
    "seed": re.compile(r"[0-9]{1,12}"),
    "gate": re.compile(r"G[0-7]-(?:pass|fail)"),
    "run": re.compile(r"[0-9a-f]{8}"),
}


class TagError(ValueError):
    pass


class TagOverflow(TagError):
    """The merged tag list would exceed the API's 10-tag limit."""


def validate(tag: str) -> str:
    """Return ``tag`` if it is a well-formed Omega tag, else raise ``TagError``."""
    if not 1 <= len(tag) <= MAX_TAG_LENGTH:
        raise TagError(f"tag length must be 1–{MAX_TAG_LENGTH}: {tag!r}")
    key, sep, value = tag.partition(":")
    rule = _RULES.get(key)
    if not sep or rule is None:
        raise TagError(f"unknown tag key in {tag!r}; known: {sorted(_RULES)}")
    if not rule.fullmatch(value):
        raise TagError(f"malformed {key} tag: {tag!r}")
    return tag


@dataclass(frozen=True)
class TagSet:
    """A trace's tags, parsed. Unrecognised or malformed tags are kept in ``other``."""

    values: dict[str, tuple[str, ...]] = field(default_factory=dict)
    other: tuple[str, ...] = ()

    @classmethod
    def parse(cls, tags: Iterable[str]) -> TagSet:
        values: dict[str, list[str]] = {}
        other: list[str] = []
        for tag in tags:
            try:
                validate(tag)
            except TagError:
                other.append(tag)
                continue
            key, _, value = tag.partition(":")
            values.setdefault(key, []).append(value)
        return cls({k: tuple(v) for k, v in values.items()}, tuple(other))

    def one(self, key: str) -> str | None:
        found = self.values.get(key, ())
        return found[-1] if found else None

    def all(self, key: str) -> tuple[str, ...]:
        return self.values.get(key, ())


def merge(current: Sequence[str], add: Iterable[str] = (), remove: Iterable[str] = ()) -> list[str]:
    """``current`` with ``remove`` dropped and ``add`` applied (order kept, no duplicates)."""
    dropped = set(remove)
    result = [tag for tag in current if tag not in dropped]
    for tag in add:
        validate(tag)
        if tag in result:
            continue
        key = tag.partition(":")[0]
        if key in SINGLE_VALUED:
            result = [t for t in result if t.partition(":")[0] != key]
        result.append(tag)
    if len(result) > MAX_TAGS:
        raise TagOverflow(f"{len(result)} tags exceed the limit of {MAX_TAGS}: {result}")
    return result


def idempotency_key(trace_id: str, revision: int, tags: Sequence[str]) -> str:
    """One logical write = one key: a retry with the same input reuses it.

    The API accepts a UUIDv4 or unpadded base64url of 128 or 256 bits (a UUIDv5 is refused,
    ``400 INVALID_IDEMPOTENCY_KEY``), so the key is the base64url SHA-256 of the write.
    """
    material = "\x1f".join(["kintsugi.tags/v1", trace_id, str(revision), *tags])
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class _Summary(Protocol):
    @property
    def tags(self) -> tuple[str, ...]: ...

    @property
    def metadata_revision(self) -> int | None: ...


class _Api(Protocol):
    def trace(self, trace_id: str) -> _Summary: ...

    def patch_trace(
        self,
        trace_id: str,
        *,
        expected_revision: int,
        tags: Sequence[str] | None = None,
        trace_name: str | None = None,
        idempotency_key: str | None = None,
    ) -> _Summary: ...


class TagWriter:
    def __init__(self, api: _Api, *, attempts: int = 5) -> None:
        self.api = api
        self.attempts = attempts
        self.conflicts = 0

    def apply(self, trace_id: str, *, add: Iterable[str] = (), remove: Iterable[str] = ()):
        """Merge ``add``/``remove`` into the trace's tags; retry on a revision conflict."""
        from kintsugi.senses.neatlogs_api import Conflict

        add, remove = list(add), list(remove)
        for _ in range(self.attempts):
            summary = self.api.trace(trace_id)
            current = list(summary.tags)
            wanted = merge(current, add, remove)
            if wanted == current:
                return summary
            revision = summary.metadata_revision or 0
            try:
                return self.api.patch_trace(
                    trace_id,
                    expected_revision=revision,
                    tags=wanted,
                    idempotency_key=idempotency_key(trace_id, revision, wanted),
                )
            except Conflict:
                self.conflicts += 1
        raise TagError(f"{trace_id}: tags still conflicting after {self.attempts} attempts")

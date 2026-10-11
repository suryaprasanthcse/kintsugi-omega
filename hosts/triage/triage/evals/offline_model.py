"""The offline stand-in for Triage's three model steps (amendment A-23).

Keyword rules over the issue text. **Tier-aware by design, and labelled as such:** the stand-in
for ``claude-haiku-*`` reads English keywords only (cognates such as "API", "SSO" or "upload"
still match), while larger tiers also read the multilingual lexicon. That gap is a declared
simulation parameter for offline runs, not a measurement of Claude: the live baseline measures
the real models once a key is configured.
"""

from __future__ import annotations

import json
import re
from typing import Any

from triage.snapshot import COMPONENTS

ENGLISH: dict[str, tuple[str, ...]] = {
    "docs": ("installation guide", "api reference", "migration guide", "cli tutorial",
             "faq page", "documentation", "docs"),
    "api": ("/v2/projects", "endpoint", "pagination", "rate limit", "webhook", "api"),
    "cli": ("hl init", "hl deploy", "hl logs", "shell completion", "config file", "cli"),
    "auth": ("sso", "token", "two-factor", "oauth", "session", "login"),
    "storage": ("upload", "bucket", "snapshot", "checksum", "storage"),
    "ui": ("dashboard", "dark mode", "settings page", "search box", "notification"),
}  # fmt: skip
INTERNATIONAL: dict[str, tuple[str, ...]] = {
    "docs": ("ドキュメント", "documentação", "dokumentation"),
    "api": ("api",),
    "cli": ("comando", "befehl"),
    "auth": ("autenticar", "connexion", "anmeldung"),
    "storage": ("アップロード", "speicher", "carga"),
    "ui": ("ダッシュボード", "modo escuro", "botão", "グラフ"),
}  # fmt: skip
QUESTION = re.compile(r"(?i)\b(how do i|how should|recommended|question)\b")
FEATURE = re.compile(r"(?i)\b(add support|allow|feature request|would like|please allow)\b")
DOCS = re.compile(r"(?i)(documentation|docs?\b|ドキュメント|obsolète)")
_OWNER = {s.lower(): c for c, subjects in COMPONENTS.items() for s in subjects}
_SUBJECTS = sorted(_OWNER, key=len, reverse=True)


def multilingual(model: str) -> bool:
    return not model.startswith("claude-haiku")


def classify_type(text: str, model: str) -> str:
    if QUESTION.search(text):
        return "question"
    if FEATURE.search(text):
        return "feature"
    if DOCS.search(text) and (multilingual(model) or re.search(r"(?i)outdated|out of date|2\.x",
                                                                 text)):  # fmt: skip
        return "docs"
    return "bug"


def classify_component(text: str, model: str) -> str:
    """The named subject's component first; keywords only when no known subject is named."""
    if (subject := subject_of(text)) is not None:
        return _OWNER[subject]
    lowered = text.lower()
    lexicons = [ENGLISH, INTERNATIONAL] if multilingual(model) else [ENGLISH]
    for lexicon in lexicons:
        for component, words in lexicon.items():  # docs first: "API reference" is docs
            if any(word in lowered for word in words):
                return component
    return "api"


def subject_of(text: str) -> str | None:
    lowered = text.lower()
    return next((s for s in _SUBJECTS if s in lowered), None)


def detect_duplicate(new: str, candidates: list[tuple[int, str]], model: str) -> int | None:
    subject, kind = subject_of(new), classify_type(new, model)
    if subject is None:
        return None
    for number, text in candidates:
        if subject_of(text) == subject and classify_type(text, model) == kind:
            return number
    return None


def _issue_block(text: str) -> str:
    """The new issue's title and body from a prompt."""
    match = re.search(r"Title: ([^\n]*)\nBody: (.*?)(?:\n\nExisting issues:|\Z)", text, re.S)
    return f"{match[1]}\n{match[2]}" if match else text


def respond(request: dict[str, Any]) -> dict[str, Any]:
    content = request["messages"][0]["content"]
    text = content if isinstance(content, str) else "".join(
        block.get("text", "") for block in content if isinstance(block, dict))  # fmt: skip
    model = str(request.get("model", ""))
    issue = _issue_block(text)
    if text.startswith("Classify the issue type"):
        answer: dict[str, Any] = {"type": classify_type(issue, model)}
    elif text.startswith("Which component"):
        answer = {"component": classify_component(issue, model)}
    else:
        existing = text.split("Existing issues:", 1)[-1]
        candidates = [(int(m[1]), f"{m[2]}\n{m[3]}") for m in
                      re.finditer(r"#(\d+)\nTitle: (.*)\nBody: (.*)", existing)]  # fmt: skip
        answer = {"duplicate_of": detect_duplicate(issue, candidates, model)}
    return {"content": [{"type": "text", "text": json.dumps(answer)}], "stop_reason": "end_turn"}

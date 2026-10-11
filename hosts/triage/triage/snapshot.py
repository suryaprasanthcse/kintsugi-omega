"""Issues: the frozen snapshot of historical issues and the generator of incoming ones.

An issue's problem is (component, subject, kind); its wording comes from one of three
phrasings. The snapshot holds 45 historical issues, each a distinct problem in phrasing 0. An
incoming issue either repeats a historical problem in another phrasing (a duplicate) or states
a problem the snapshot does not have. A small bank of issues has titles in other languages and
a generic English body, so the component can only be read from the title.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from functools import cache

COMPONENTS: dict[str, tuple[str, ...]] = {
    "api": ("the /v2/projects endpoint", "the pagination cursor", "rate limit headers",
            "webhook payloads", "the bulk export endpoint"),
    "cli": ("the hl init command", "hl deploy --dry-run", "shell completion",
            "config file parsing", "hl logs --follow"),
    "auth": ("SSO login", "API token refresh", "the two-factor prompt", "the OAuth callback",
             "session timeout"),
    "storage": ("large file uploads", "bucket listing", "snapshot restore",
                "checksum verification", "multipart uploads"),
    "ui": ("the dashboard charts", "the dark mode toggle", "the settings page",
           "the project search box", "the notification bell"),
    "docs": ("the installation guide", "the API reference", "the v2 migration guide",
             "the CLI tutorial", "the FAQ page"),
}  # fmt: skip
KINDS = ("bug", "feature", "question", "docs")
PHRASINGS: dict[str, tuple[tuple[str, str], ...]] = {
    "bug": (("{S} fails with an internal error", "Since 2.3.0, {s} fails with an internal "
             "error every time. Logs show a stack trace from the server."),
            ("Error when using {s}", "Using {s} gives an error after upgrading to 2.3. It worked "
             "on 2.2. Reproducible on two machines."),
            ("Getting an internal error from {s}", "We keep getting an internal error from {s}; "
             "no workaround found yet.")),
    "feature": (("Add support for exporting {s} settings", "It would help if {s} settings "
                 "could be exported and imported between projects."),
                ("Allow exporting the configuration of {s}", "Please allow exporting what {s} "
                 "is configured with, so teams can share it."),
                ("Feature request: share {s} settings across projects", "We would like to "
                 "share {s} settings between projects instead of repeating them.")),
    "question": (("How do I configure {s} for a team?", "What is the right way to configure "
                  "{s} for a team of ten? The docs do not say."),
                 ("What is the recommended setup for {s} in a team?", "Is there a recommended "
                  "way to set up {s} for a whole team?"),
                 ("Question: team configuration of {s}", "How should a team configure {s}? "
                  "Looking for guidance.")),
    "docs": (("Documentation for {s} is outdated", "The documentation describing {s} still "
              "shows the 1.x behaviour and screenshots."),
             ("Docs: {s} section needs an update for 2.x", "The section about {s} has not been "
              "updated for 2.x and confuses new users."),
             ("Outdated docs about {s}", "Docs about {s} are out of date with the current "
              "release.")),
}  # fmt: skip
MIXED: tuple[tuple[str, str, str], ...] = (
    ("api", "bug", "La API devuelve un error 500 al paginar proyectos"),
    ("api", "bug", "API liefert Fehler 500 beim Blättern durch Projekte"),
    ("cli", "bug", "El comando hl init se cierra sin mensaje"),
    ("cli", "bug", "CLI-Befehl hl deploy hängt ohne Ausgabe"),
    ("auth", "bug", "Erro ao autenticar com SSO após atualização"),
    ("auth", "bug", "La connexion SSO échoue après la mise à jour"),
    ("storage", "bug", "Speicher-Upload bricht bei großen Dateien ab"),
    ("storage", "bug", "大きなファイルのアップロードが途中で失敗する"),
    ("ui", "bug", "ダッシュボードのグラフが表示されない"),
    ("ui", "bug", "O botão de modo escuro não funciona"),
    ("docs", "docs", "La documentation d'installation est obsolète"),
    ("docs", "docs", "ドキュメントの移行ガイドが古い"),
)
MIXED_BODY = "See title. Version 2.3.1, reproducible every time."


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    body: str
    component: str
    kind: str
    subject: str  # "" for mixed-language issues
    duplicate_of: int | None = None
    language: str = "en"

    def as_public(self) -> dict[str, object]:
        """What GitHub shows: no gold fields."""
        return {"number": self.number, "title": self.title, "body": self.body}


def _render(component: str, kind: str, subject: str, phrasing: int, number: int) -> Issue:
    title, body = PHRASINGS[kind][phrasing]
    fill = {"s": subject, "S": subject[0].upper() + subject[1:]}
    return Issue(number, title.format(**fill), body.format(**fill), component, kind, subject)


@cache
def problems() -> tuple[tuple[str, str, str], ...]:
    """Every (component, subject, kind) problem: documentation issues belong to ``docs``; the
    other components get bugs, feature requests and questions (80 problems)."""
    rows = []
    for component, subjects in COMPONENTS.items():
        kinds = ("docs",) if component == "docs" else ("bug", "feature", "question")
        rows += [(component, subject, kind) for subject in subjects for kind in kinds]
    return tuple(rows)


SNAPSHOT_SIZE = 45


@cache
def snapshot() -> tuple[Issue, ...]:
    """The historical issues (#1-#45): distinct problems, phrasing 0."""
    rng = random.Random("triage:snapshot")
    chosen = rng.sample(problems(), SNAPSHOT_SIZE)
    return tuple(_render(c, k, s, 0, n) for n, (c, s, k) in enumerate(chosen, 1))


def incoming(rng: random.Random, n: int, first_number: int) -> list[Issue]:
    """``n`` new issues: ~20% duplicates of the snapshot, ~10% mixed-language, the rest new."""
    historical = {(i.component, i.subject, i.kind) for i in snapshot()}
    fresh = [p for p in problems() if p not in historical]
    issues = []
    for offset in range(n):
        number, roll = first_number + offset, rng.random()
        if roll < 0.10:
            component, kind, title = rng.choice(MIXED)
            language = "ja" if any("぀" <= ch <= "ヿ" or "一" <= ch <= "鿿"
                                   for ch in title) else "other"  # fmt: skip
            issues.append(Issue(number, title, MIXED_BODY, component, kind, "",
                                language=language))  # fmt: skip
        elif roll < 0.30:
            original = rng.choice(snapshot())
            twin = _render(original.component, original.kind, original.subject,
                           rng.choice((1, 2)), number)  # fmt: skip
            issues.append(Issue(twin.number, twin.title, twin.body, twin.component, twin.kind,
                                twin.subject, duplicate_of=original.number))  # fmt: skip
        else:
            component, subject, kind = rng.choice(fresh)
            issues.append(_render(component, kind, subject, rng.randrange(3), number))
    return issues

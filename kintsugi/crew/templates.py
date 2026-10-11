"""Hashed prompt templates for the crew (spec §4.5.4, §5.14; amendment A-17).

Every crew prompt is generated from incident data by a template, never written by hand at run
time. A template is one file in ``kintsugi/crew/prompts/<name>.md``: a system part, a line
``---- user ----``, then the user part. Placeholders are ``$name`` (:class:`string.Template`).

- The **template hash** is the SHA-256 of the file's bytes. ``prompts/SHA256SUMS`` pins every
  template, and a test fails when a template changes without its pin, so a prompt change is
  always a visible, reviewed diff.
- The **prompt hash** is the SHA-256 of the exact rendered user prompt. For a Surgeon it is the
  ``Kintsugi-Prompt`` trailer and the attestation's ``prompt_template_sha`` (A-17); for the other
  roles it is recorded with the call in the incident's case file.

Rendering is strict: a missing or an unknown field raises, so no prompt ships with a literal
``$placeholder`` in it or silently drops incident data.
"""

from __future__ import annotations

import hashlib
import string
from dataclasses import dataclass
from functools import cache
from pathlib import Path

PROMPTS = Path(__file__).with_name("prompts")
PINS = PROMPTS / "SHA256SUMS"
SEPARATOR = "\n---- user ----\n"


class TemplateError(ValueError):
    pass


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Template:
    name: str
    system: str
    user: str
    sha256: str  # "sha256:<hex>" of the template file's bytes

    @property
    def fields(self) -> frozenset[str]:
        found: set[str] = set()
        for part in (self.system, self.user):
            found.update(string.Template(part).get_identifiers())
        return frozenset(found)


@dataclass(frozen=True)
class Rendered:
    template: str
    template_sha: str
    system: str
    prompt: str

    @property
    def prompt_sha(self) -> str:
        return sha256_text(self.prompt)


def names() -> list[str]:
    return sorted(path.stem for path in PROMPTS.glob("*.md"))


@cache
def load(name: str) -> Template:
    path = PROMPTS / f"{name}.md"
    if not path.is_file():
        raise TemplateError(f"no prompt template {name!r}")
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    if SEPARATOR not in text:
        raise TemplateError(f"{path.name}: missing the '---- user ----' separator")
    system, user = text.split(SEPARATOR, 1)
    return Template(name, system.strip(), user.strip() + "\n",
                    "sha256:" + hashlib.sha256(raw).hexdigest())  # fmt: skip


def render(name: str, **values: object) -> Rendered:
    """Fill every placeholder of ``name`` exactly once; values are converted with ``str``."""
    template = load(name)
    given = set(values)
    missing, unknown = template.fields - given, given - template.fields
    if missing or unknown:
        raise TemplateError(f"{name}: missing {sorted(missing)}, unknown {sorted(unknown)}")
    text = {key: str(value) for key, value in values.items()}
    try:
        system = string.Template(template.system).substitute(text)
        prompt = string.Template(template.user).substitute(text)
    except (KeyError, ValueError) as error:  # a stray "$" in the template itself
        raise TemplateError(f"{name}: {error}") from None
    return Rendered(name, template.sha256, system, prompt)


def pins() -> dict[str, str]:
    """``prompts/SHA256SUMS``: ``<hex>  <name>.md`` per line."""
    pinned: dict[str, str] = {}
    for line in PINS.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            digest, _, filename = line.partition("  ")
            pinned[filename.strip().removesuffix(".md")] = "sha256:" + digest.strip()
    return pinned


def check_pins() -> list[str]:
    """Templates whose bytes differ from their pin, and pins without a template."""
    pinned = pins()
    problems = [f"{name}: not pinned" for name in names() if name not in pinned]
    problems += [f"{name}: pinned but missing" for name in pinned if name not in names()]
    problems += [f"{name}: hash changed (re-pin after review)" for name in names()
                 if name in pinned and pinned[name] != load(name).sha256]  # fmt: skip
    return problems


def pin_file() -> str:
    """The text ``prompts/SHA256SUMS`` should hold for the templates on disk."""
    header = "# KINTSUGI-Omega crew prompt templates (spec 4.5.4): sha256 of each template file.\n"
    lines = [f"{load(n).sha256.removeprefix('sha256:')}  {n}.md" for n in names()]
    return header + "\n".join(lines) + "\n"

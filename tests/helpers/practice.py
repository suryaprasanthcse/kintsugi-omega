"""The Ledger practice repository: a disposable fixture target for gate, crew and rehearsal tests.

``make_repo`` copies ``tests/fixtures/rehearsal/ledger_practice`` into a scratch git repository
(healthy commit, then a sealed holdout under a throwaway age identity). ``plant_refactor`` makes
the disclosed practice regression: the "readability" refactor of ``reconcile`` that constructs
the posting session inside the retried closure. ``hoist_fix`` and ``lookup_fix`` are two repairs
the gates must tell apart.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from kintsugi.gates import holdout
from kintsugi.gates.target import Target, load_target
from kintsugi.proof.sealed import new_identity
from kintsugi.sandbox import proc

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests" / "fixtures" / "rehearsal" / "ledger_practice"
REFACTOR = REPO / "tests" / "fixtures" / "rehearsal" / "refactor" / "reconcile.py"
HOST = "ledger-practice"
RECONCILE = "practice_ledger/reconcile.py"
IDENTITY = ("-c", "user.name=Surya Prasanth R", "-c", "user.email=suryaprasanthcse@gmail.com")


def git(repo: Path, *args: str) -> str:
    return proc.run(["git", "-C", str(repo), *args], timeout_s=120, check=True).stdout.strip()


def commit_all(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, *IDENTITY, "commit", "-q", "--no-verify", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@dataclass(frozen=True)
class Practice:
    repo: Path
    healthy: str
    identity: str  # the throwaway holdout identity (never a real key)

    @property
    def target(self) -> Target:
        return load_target(self.repo, HOST)


def make_repo(destination: Path) -> Practice:
    shutil.copytree(FIXTURE, destination, ignore=shutil.ignore_patterns("__pycache__"))
    git(destination, "init", "-q", "-b", "main")
    git(destination, "config", "core.autocrlf", "false")
    commit_all(destination, "practice: the healthy ledger")
    secret, recipient = new_identity()
    target = load_target(destination, HOST)
    target.make_importable()
    holdout.seal(destination, HOST, "practice_ledger", destination, recipient=recipient)
    healthy = commit_all(destination, "practice: seal the holdout")
    return Practice(destination, healthy, secret)


def write(repo: Path, relative: str, text: str) -> None:
    (repo / relative).write_text(text, encoding="utf-8", newline="\n")


def plant_refactor(repo: Path) -> str:
    write(repo, RECONCILE, REFACTOR.read_text(encoding="utf-8"))
    return commit_all(repo, "refactor(reconcile): name the retried pass and document it")


DOC = ('    """Book every pending line of ``batch`` in USD; a rate limit retries the whole '
       'pass."""\n')  # fmt: skip
HOISTED = (
    DOC + "    current = session or PostingSession(batch)  # outside the retried scope\n\n"
    "    def book_pending_lines() -> PostingSession:\n"
)
NESTED = (
    DOC + "\n    def book_pending_lines() -> PostingSession:\n"
    "        current = session or PostingSession(batch)\n"
)


def hoist_fix(repo: Path) -> str:
    text = (repo / RECONCILE).read_text(encoding="utf-8")
    write(repo, RECONCILE, text.replace(NESTED, HOISTED))
    return commit_all(repo, "fix: construct the posting session outside the retried pass")


LOOKUP = (
    "    def book(self, line: Line, rate: Decimal) -> None:\n"
    "        if line.key not in self.booked and not already_booked(line.key):\n"
)


def lookup_fix(repo: Path) -> str:
    """Idempotency by asking the books API before every line: correct, but slower and costlier."""
    text = (repo / RECONCILE).read_text(encoding="utf-8")
    text = text.replace(
        "    def book(self, line: Line, rate: Decimal) -> None:\n"
        "        if line.key not in self.booked:\n",
        LOOKUP,
    )
    text = text.replace(
        "from practice_ledger.tools import fx_rate, post_line",
        "from practice_ledger.tools import already_booked, fx_rate, post_line",
    )
    write(repo, RECONCILE, text)
    tools = (repo / "practice_ledger/tools.py").read_text(encoding="utf-8")
    tools += (
        '\n\n@span(kind="TOOL", name="already_booked")\n'
        "def already_booked(key: str) -> bool:\n"
        '    response = external.call("books", "GET", f"/lines/{key}")\n'
        "    return response.status_code == 200\n"
    )
    write(repo, "practice_ledger/tools.py", tools)
    return commit_all(repo, "fix: check the books before booking a line")

"""Ship: issue → pull request → G7 → merge, with the repair receipt (spec §5.13, §6.5 step 10).

``GitHubShipper.ship(winner)``:

1. pushes the winner's attested commit as a branch (never force) together with the incident's
   attempt refs and the checkpoint refs their trailers name, after the secret scan of the
   checkpoint trees (amendment A-19);
2. opens (or finds) the issue ``[INC-…] <symptom label>`` with the symptom and its traces;
3. opens (or finds) the pull request ``fix(INC-…): <card title>``, ``Fixes #n``. Its body names
   the repro tasks (``Kintsugi-Repro-Tasks:``) that G7 re-runs at fresh seeds;
4. waits for the G7 check (``.github/workflows/g7-gates.yml``) to finish;
5. rewrites the body as the **E.4 repair receipt** with the G7 run linked, so every ✓ in it is
   already true, then merges with a **merge commit**: GitHub's rebase and squash merges rewrite
   commits, which would orphan the attestation signed over the commit's tree and parent (A-51).
   A merge the repository's ruleset refuses is reported as ``merged=False``.

``LocalShipper`` is the offline variant: G7 runs locally (``kintsugi.gates.g7``) at a fresh seed
and the branch is merged with ``git merge --no-ff`` in the target repository.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from kintsugi.control import contracts as c
from kintsugi.crew.casefile import CaseView
from kintsugi.crew.context import CrewContext
from kintsugi.memory import entire
from kintsugi.proof import redaction, schemas
from kintsugi.sandbox import proc

REPRO_TRAILER = "Kintsugi-Repro-Tasks"
GH_WINDOWS = Path("C:/Program Files/GitHub CLI/gh.exe")


class ShipError(RuntimeError):
    pass


# ---- GitHub ---------------------------------------------------------------------------------


class GitHub(Protocol):
    def issue_find(self, marker: str) -> int | None: ...

    def issue_create(self, title: str, body: str) -> int: ...

    def pr_find(self, head: str) -> dict[str, Any] | None: ...

    def pr_create(self, base: str, head: str, title: str, body: str) -> dict[str, Any]: ...

    def pr_edit_body(self, number: int, body: str) -> None: ...

    def pr_checks(self, number: int) -> list[dict[str, Any]]: ...

    def pr_merge(self, number: int, method: str) -> str | None: ...


def gh_executable() -> str:
    found = proc.which("gh")
    if found:
        return found
    if GH_WINDOWS.is_file():
        return str(GH_WINDOWS)
    raise ShipError("the GitHub CLI (gh) is not installed")


class GhCli:
    """The GitHub port through the ``gh`` CLI; the token reaches only the child (``GH_TOKEN``)."""

    def __init__(self, repo: str, *, token: str | None = None, gh: str | None = None) -> None:
        self.repo = repo
        self.gh = gh or gh_executable()
        self.env = {"GH_TOKEN": token} if token else {}

    def _run(self, *args: str, input_text: str | None = None, check: bool = True) -> str:
        result = proc.run(
            [self.gh, *args], extra_env=self.env, input_text=input_text, timeout_s=180
        )
        if check and not result.ok:
            raise ShipError(f"gh {' '.join(args[:2])} failed: {result.stderr.strip()[-500:]}")
        return result.stdout

    def issue_find(self, marker: str) -> int | None:
        out = self._run(
            "issue",
            "list",
            "--repo",
            self.repo,
            "--state",
            "all",
            "--search",
            f'"{marker}" in:title',
            "--json",
            "number,title",
        )
        for issue in json.loads(out or "[]"):
            if marker in issue["title"]:
                return int(issue["number"])
        return None

    def issue_create(self, title: str, body: str) -> int:
        url = self._run(
            "issue",
            "create",
            "--repo",
            self.repo,
            "--title",
            title,
            "--body-file",
            "-",
            input_text=body,
        ).strip()
        return int(url.rstrip("/").rsplit("/", 1)[-1])

    def pr_find(self, head: str) -> dict[str, Any] | None:
        out = self._run(
            "pr",
            "list",
            "--repo",
            self.repo,
            "--head",
            head,
            "--state",
            "all",
            "--json",
            "number,url,state",
        )
        found = json.loads(out or "[]")
        return dict(found[0]) if found else None

    def pr_create(self, base: str, head: str, title: str, body: str) -> dict[str, Any]:
        url = self._run(
            "pr",
            "create",
            "--repo",
            self.repo,
            "--base",
            base,
            "--head",
            head,
            "--title",
            title,
            "--body-file",
            "-",
            input_text=body,
        ).strip()
        return {"number": int(url.rstrip("/").rsplit("/", 1)[-1]), "url": url}

    def pr_edit_body(self, number: int, body: str) -> None:
        self._run(
            "pr", "edit", str(number), "--repo", self.repo, "--body-file", "-", input_text=body
        )

    def pr_checks(self, number: int) -> list[dict[str, Any]]:
        # Exit code 8 means "checks pending": the JSON is still printed.
        out = self._run(
            "pr",
            "checks",
            str(number),
            "--repo",
            self.repo,
            "--json",
            "name,state,bucket,link",
            check=False,
        )
        try:
            return list(json.loads(out or "[]"))
        except json.JSONDecodeError:
            return []

    def pr_merge(self, number: int, method: str) -> str | None:
        result = proc.run(
            [self.gh, "pr", "merge", str(number), "--repo", self.repo, f"--{method}"],
            extra_env=self.env,
            timeout_s=180,
        )
        if not result.ok:
            return None
        view = self._run(
            "pr", "view", str(number), "--repo", self.repo, "--json", "mergeCommit,state"
        )
        data = json.loads(view)
        commit = data.get("mergeCommit") or {}
        return str(commit.get("oid")) if data.get("state") == "MERGED" and commit else None


# ---- the receipt ------------------------------------------------------------------------------


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.1%}"


def _signed(x: float | None) -> str:
    return "n/a" if x is None else f"{x:+.1%}"


def receipt_values(
    view: CaseView,
    ctx: CrewContext,
    out: c.SurgeonOutcome,
    *,
    issue: int,
    ci_url: str,
    ledger: tuple[int, str],
    detected: str,
) -> dict[str, Any]:
    card = view.get("card", out.hypothesis)
    verdict = view.get("verdict", out.hypothesis)
    symptom = view.get("symptom", "summary")
    record = view.get("intent_record", "record", None) or {}
    report = view.get("cause", "report", None) or {}
    plan = view.get("plan", "plan", None) or {}
    g4 = verdict.get("g4") or {}
    clauses = record.get("behavioral_contract") or []
    quoted = (clauses[0]["quote"] if clauses else record.get("stated_goal", ""))[:300]
    checkpoints = []
    for hyp in view.names("outcome"):
        outcome = view.get("outcome", hyp)
        checkpoints.append(f"{hyp} `{outcome.get('checkpoint') or 'no-checkpoint'}`")
    signature = view.get("signature", "phi", None) or {}
    traces = ", ".join(signature.get("trace_ids", [])[:5]) or "(local rehearsal traces)"
    case = ctx.case(out.incident_id)
    return {
        "INC": out.incident_id,
        "symptom_summary": str(symptom["summary"])[:800],
        "host": str(card["host"]),
        "t_detect": detected,
        "t_resolve": datetime.now(UTC).isoformat(timespec="seconds"),
        "suspects": ", ".join(report.get("suspects") or []) or "(see cause report)",
        "ck": str(record.get("checkpoint") or "no-checkpoint"),
        "quoted_intent": quoted,
        "fix_description": f"{card['title']}: {card['approach']}"[:500],
        "antibody": "primary",
        "p_before": _pct(g4.get("pass_before")),
        "ci_before": _ci(g4.get("ci_before")),
        "p_after": _pct(g4.get("pass_after")),
        "ci_after": _ci(g4.get("ci_after")),
        "p": f"{float(g4.get('p', 1.0)):.3g}",
        "c_before": f"${float(g4.get('cost_before') or 0):.5f}",
        "c_after": f"${float(g4.get('cost_after') or 0):.5f}",
        "cost_ub": _signed(g4.get("cost_ub")),
        "l_before": f"{float(g4.get('p95_before') or 0):.3f} s",
        "l_after": f"{float(g4.get('p95_after') or 0):.3f} s",
        "p95_ub": _signed(g4.get("p95_ub")),
        "ci_url": ci_url,
        "trace_links_before": traces,
        "trace_links_after": "canary traces follow the merge",
        "checkpoints": "; ".join(checkpoints) or "none",
        "cost": f"{case.spent():.2f}",
        "budget": f"{float(plan.get('budget_usd', 0)):.2f}",
        "plan_sha": str(plan.get("plan_sha", "")),
        "seq": max(1, ledger[0]),
        "h": ledger[1],
        "issue": issue,
    }


def _ci(pair: Any) -> str:
    if not pair:
        return "n/a"
    low, high = pair
    return f"{low:.2f}–{high:.2f}"


def render_receipt(values: Mapping[str, Any], repro_tasks: list[str]) -> str:
    problems = schemas.errors("E4", dict(values))
    if problems:
        raise ShipError(f"the E.4 receipt does not validate: {problems}")
    body = schemas.receipt_template().format(**values)
    return body.rstrip() + f"\n\n{REPRO_TRAILER}: {','.join(repro_tasks)}\n"


# ---- shippers ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class ShipConfig:
    github_repo: str  # owner/name
    remote: str = "origin"
    base_branch: str = "main"
    branch_prefix: str = ""  # rehearsals: "rehearsal/<run>/"
    g7_check: str = "G7"
    g7_timeout_s: float = 3600.0
    poll_s: float = 20.0
    merge_method: str = "merge"  # A-51: keeps the attested commit


def _git(repo: Path, *args: str, timeout_s: float = 300.0) -> str:
    return proc.run(["git", "-C", str(repo), *args], timeout_s=timeout_s, check=True).stdout


class GitHubShipper:
    def __init__(
        self,
        ctx: CrewContext,
        config: ShipConfig,
        github: GitHub,
        *,
        ledger_head: Callable[[], tuple[int, str]],
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.ctx = ctx
        self.config = config
        self.github = github
        self.ledger_head = ledger_head
        self.sleep = sleep

    def _push(self, repo: Path, out: c.SurgeonOutcome, branch: str, view: CaseView) -> None:
        refs = [f"{out.commit_sha}:refs/heads/{branch}"]
        attempts = _git(
            repo, "for-each-ref", "--format=%(refname)", f"refs/kintsugi/attempts/{out.incident_id}"
        ).split()
        refs += [f"{ref}:{ref}" for ref in attempts]
        checkpoints = []
        for hyp in view.names("outcome"):
            checkpoint = view.get("outcome", hyp).get("checkpoint")
            if checkpoint:
                ref = entire.checkpoint_ref(str(checkpoint))
                if proc.run(
                    ["git", "-C", str(repo), "show-ref", "--verify", "-q", ref], timeout_s=30
                ).ok:
                    checkpoints.append(ref)
        if checkpoints:
            redaction.assert_pushable(repo, checkpoints)  # transcripts become public (A-19)
        refs += [f"{ref}:{ref}" for ref in checkpoints]
        _git(repo, "push", self.config.remote, *refs)

    def ship(self, out: c.SurgeonOutcome) -> c.Shipment:
        if out.commit_sha is None:
            raise ShipError(f"{out.hypothesis} has no attested commit to ship")
        cfg = self.config
        case = self.ctx.case(out.incident_id)
        view = case.reader("shipper")
        card = view.get("card", out.hypothesis)
        target = self.ctx.target(str(card["host"]))
        branch = cfg.branch_prefix + out.branch
        base = cfg.branch_prefix + cfg.base_branch
        self._push(target.repo, out, branch, view)
        symptom = view.get("symptom", "summary")
        marker = f"[{out.incident_id}]"
        issue = self.github.issue_find(marker) or self.github.issue_create(
            f"{marker} {symptom.get('label') or 'failure'} on {card['host']}",
            f"Kintsugi-Omega incident {out.incident_id} on host `{card['host']}`.\n\n"
            f"**Symptom:** {symptom['summary']}\n",
        )
        repro = view.get("repro", "g0", None) or {}
        tasks = list(repro.get("failing") or repro.get("tasks") or [])
        pr = self.github.pr_find(branch) or self.github.pr_create(
            base,
            branch,
            f"fix({out.incident_id}): {card['title']}"[:120],
            f"Kintsugi repair for {out.incident_id} ({out.hypothesis}). The repair receipt "
            f"replaces this text once G7 has run.\n\nFixes #{issue}\n\n"
            f"{REPRO_TRAILER}: {','.join(tasks)}\n",
        )
        number, url = int(pr["number"]), str(pr.get("url", ""))
        g7_ok, ci_url = self._wait_g7(number)
        values = receipt_values(
            view,
            self.ctx,
            out,
            issue=issue,
            ci_url=ci_url,
            ledger=self.ledger_head(),
            detected=_detected(case.root),
        )
        if g7_ok:
            self.github.pr_edit_body(number, render_receipt(values, tasks))
        case.put(
            "decision",
            f"ship-{out.hypothesis}",
            {"issue": issue, "pr": url, "g7_ok": g7_ok, "g7": ci_url},
            by="shipper",
        )
        if not g7_ok:
            return c.Shipment(out.incident_id, out.hypothesis, branch, url, False, False)
        merge = self.github.pr_merge(number, cfg.merge_method)
        if merge:
            _git(
                target.repo,
                "fetch",
                cfg.remote,
                f"+refs/heads/{base}:refs/kintsugi/shipped/{out.incident_id}",
            )
        return c.Shipment(
            out.incident_id, out.hypothesis, branch, url, True, merge is not None, merge
        )

    def _wait_g7(self, number: int) -> tuple[bool, str]:
        deadline = time.monotonic() + self.config.g7_timeout_s
        while True:
            checks = [
                k
                for k in self.github.pr_checks(number)
                if self.config.g7_check in str(k.get("name"))
            ]
            done = [
                k for k in checks if str(k.get("bucket")) in ("pass", "fail", "cancel", "skipping")
            ]
            if checks and len(done) == len(checks):
                ok = all(str(k.get("bucket")) == "pass" for k in checks)
                return ok, str(checks[0].get("link") or "")
            if time.monotonic() > deadline:
                return False, "G7 timed out"
            self.sleep(self.config.poll_s)


class LocalShipper:
    """Offline shipping: G7 locally at a fresh seed, then ``git merge --no-ff`` on main."""

    def __init__(
        self,
        ctx: CrewContext,
        *,
        seed: int,
        identity: Path | None,
        ledger_head: Callable[[], tuple[int, str]],
    ) -> None:
        self.ctx = ctx
        self.seed = seed
        self.identity = identity
        self.ledger_head = ledger_head

    def ship(self, out: c.SurgeonOutcome) -> c.Shipment:
        from kintsugi.gates.g7 import run_g7

        if out.commit_sha is None:
            raise ShipError(f"{out.hypothesis} has no attested commit to ship")
        case = self.ctx.case(out.incident_id)
        view = case.reader("shipper")
        card = view.get("card", out.hypothesis)
        target = self.ctx.target(str(card["host"]))
        repro = view.get("repro", "g0", None) or {}
        base = _git(target.repo, "rev-parse", self.ctx.base_ref).strip()
        identity = (
            self.identity.read_text(encoding="ascii").strip() if self.identity is not None else None
        )
        summary = run_g7(
            target,
            base,
            out.commit_sha,
            seed=self.seed,
            task_ids=list(repro.get("failing") or []),
            identity=identity,
        )
        del identity
        url = f"local://g7/{out.incident_id}/{out.hypothesis}"
        values = receipt_values(
            view,
            self.ctx,
            out,
            issue=1,
            ci_url=url,
            ledger=self.ledger_head(),
            detected=_detected(case.root),
        )
        receipt = render_receipt(values, list(repro.get("failing") or []))
        case.put(
            "decision", f"ship-{out.hypothesis}", {"g7": summary, "receipt": receipt}, by="shipper"
        )
        if not summary["passed"]:
            return c.Shipment(out.incident_id, out.hypothesis, out.branch, url, False, False)
        _git(
            target.repo,
            "merge",
            "--no-ff",
            "-q",
            "-m",
            f"Merge {out.branch} ({out.incident_id}): {card['title']}",
            out.branch,
        )
        merge = _git(target.repo, "rev-parse", "HEAD").strip()
        return c.Shipment(out.incident_id, out.hypothesis, out.branch, url, True, True, merge)


def _detected(root: Path) -> str:
    stamps = [p.stat().st_mtime for p in root.rglob("*.json")] or [time.time()]
    return datetime.fromtimestamp(min(stamps), UTC).isoformat(timespec="seconds")


def parse_repro_tasks(body: str) -> list[str]:
    found = re.search(rf"^{REPRO_TRAILER}:\s*(.*)$", body or "", re.MULTILINE)
    return [t for t in (found.group(1).split(",") if found else []) if t.strip()]

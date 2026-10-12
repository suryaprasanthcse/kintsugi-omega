"""L3: the auto-revert of a fix the canary rejected (spec §6.7 step 16; plan §2a L3).

``Reverter.revert(shipment, …)``:

1. **Decide** the revert with git itself: ``git revert --no-commit -m 1 <merge>`` in a scratch
   worktree of the current base gives the exact tree a revert must produce.
2. **Commit** it through a notary session (``kintsugi.crew.notary``): the session applies the
   revert's patch with Edit/Write, so the commit carries an attestation and an Entire
   checkpoint (C4); a tree other than git's is refused. Branch ``kintsugi/revert/<record>``.
3. **Expedited gates**: G1 (build and static) on the revert, and G2 against the host's
   pre-vaccination behaviour: the canary's failing runs (seed and task, so the chaos is
   identical, A-24) must pass on the reverted code.
4. **Ship**: through GitHub a pull request *"Revert …"* whose body names the rejection, the
   gates and the notary; G7 when the base requires it; a merge commit (A-51). Offline, a local
   ``--no-ff`` merge into the base branch.

The record goes to the ledger and the antibody's lineage; the reverse canary (the caller runs
it on the revert's merge) confirms the host is back to its baseline.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kintsugi.control import contracts as c
from kintsugi.crew.notary import Notary, NotaryRequest
from kintsugi.gates import g1
from kintsugi.gates.checkout import SCRATCH_IDENTITY, clean_checkout
from kintsugi.gates.evals import run_tasks
from kintsugi.gates.target import Target
from kintsugi.sandbox import proc, worktree


class RevertError(RuntimeError):
    pass


@dataclass(frozen=True)
class RevertConfig:
    base_branch: str = "main"  # the branch the reverted merge landed on (without the prefix)
    branch_prefix: str = ""  # rehearsals: "rehearsal/<run>/"
    remote: str = "origin"
    require_g7: bool = True
    g7_check: str = "G7"
    g7_timeout_s: float = 3600.0
    poll_s: float = 20.0
    merge_method: str = "merge"  # A-51


@dataclass(frozen=True)
class RevertRecord:
    ok: bool
    record: str  # REV-… (names the worktree, branch and attestation)
    reverted: str  # the merge commit reverted
    commit: str | None = None
    checkpoint: str | None = None
    branch: str | None = None
    pr_url: str | None = None
    merge_sha: str | None = None
    gates: Mapping[str, Any] = field(default_factory=dict)
    reason: str = ""
    cost_usd: float = 0.0

    def shipment(self, incident: str) -> c.Shipment:
        """The revert as a shipment, for the reverse canary."""
        return c.Shipment(incident, "revert", self.branch or "", self.pr_url, self.ok,
                          self.merge_sha is not None, self.merge_sha)  # fmt: skip


def _git(repo: Path, *args: str, timeout_s: float = 300.0) -> str:
    return proc.run(["git", "-C", str(repo), *args], timeout_s=timeout_s, check=True).stdout


def revert_tree(repo: Path, base: str, merge: str) -> str:
    """The tree ``git revert -m 1 <merge>`` produces on ``base`` (raises on a conflict)."""
    with clean_checkout(repo, base) as scratch:
        result = proc.run(["git", "-C", str(scratch), *SCRATCH_IDENTITY, "revert", "--no-commit",
                           "-m", "1", merge], timeout_s=120)  # fmt: skip
        if not result.ok:
            raise RevertError(f"git revert -m 1 {merge[:12]} conflicts on {base[:12]}: "
                              f"{result.stderr.strip()[-300:]}")  # fmt: skip
        return _git(scratch, "write-tree").strip()


def revert_patch(repo: Path, merge: str) -> str:
    """The patch that undoes ``merge`` against its first parent."""
    return _git(repo, "diff", "--no-color", merge, f"{merge}^1")


def regression_check(
    target: Target, checkout: Path, cases: Sequence[tuple[int, str]]
) -> dict[str, Any]:
    """The expedited G2: every (seed, task) the canary saw fail must pass here."""
    import importlib

    target.make_importable()
    pool = {t["id"]: t for t in importlib.import_module(
        f"{target.host.package}.evals.generate").tasks("train")}  # fmt: skip
    by_seed: dict[int, list[str]] = {}
    for seed, task in cases:
        if task in pool:
            by_seed.setdefault(seed, []).append(task)
    still: dict[str, str] = {}
    for seed, ids in sorted(by_seed.items()):
        runs = run_tasks(target, checkout, [pool[i] for i in sorted(set(ids))], seed=seed,
                         phase="revert").runs  # fmt: skip
        still.update({f"{r.task}@{seed}": r.verdict for r in runs if not r.passed})
    cases_run = sum(len(set(v)) for v in by_seed.values())
    return {"gate": "G2-regression", "passed": not still and cases_run > 0, "cases": cases_run,
            "still_failing": still}  # fmt: skip


class Reverter:
    def __init__(
        self,
        notary: Notary,
        config: RevertConfig,
        *,
        github: Any = None,  # kintsugi.control.ship.GhCli (or a fake) for pull requests
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.notary = notary
        self.config = config
        self.github = github
        self.sleep = sleep

    def revert(
        self,
        target: Target,
        shipment: c.Shipment,
        *,
        record: str,
        title: str,
        why: str,
        cases: Sequence[tuple[int, str]],
    ) -> RevertRecord:
        cfg = self.config
        repo = target.repo
        merge = shipment.merge_sha
        if merge is None:
            raise RevertError(f"{shipment.incident_id}: nothing was merged, nothing to revert")
        base_ref = cfg.branch_prefix + cfg.base_branch
        if self.github is not None:
            _git(repo, "fetch", "-q", cfg.remote,
                 f"+refs/heads/{base_ref}:refs/kintsugi/base/{record}")  # fmt: skip
            base = _git(repo, "rev-parse", f"refs/kintsugi/base/{record}").strip()
        else:
            base = _git(repo, "rev-parse", base_ref).strip()
        expected = revert_tree(repo, base, merge)
        wt = worktree.create(repo, record, "revert", base=base)
        try:
            notarised = self.notary.commit(
                wt.path,
                NotaryRequest(
                    operation="revert",
                    reference=f"merge {merge[:12]} ({title})",
                    incident=record,
                    purpose=(
                        f"{why} This patch is `git revert -m 1 {merge[:12]}`: it restores "
                        "the code exactly as it was before that merge."
                    ),
                    diff=revert_patch(repo, merge),
                    subject=f"Revert {title}"[:100],
                    body=f"This reverts merge commit {merge}, restoring its first parent.\n\n{why}",
                    expected_tree=expected,
                ),
                package_root=target.package_root,
            )
        finally:
            worktree.release_lock(wt)
        gates = self._gates(target, notarised.sha, cases)
        passed = all(bool(g.get("passed")) for g in gates.values())
        branch = cfg.branch_prefix + wt.branch
        common = {"record": record, "reverted": merge, "commit": notarised.sha,
                  "checkpoint": notarised.checkpoint_id, "branch": branch, "gates": gates,
                  "cost_usd": notarised.cost_usd}  # fmt: skip
        if not passed:
            return RevertRecord(False, reason="expedited gates failed", **common)
        if self.github is None:
            _git(repo, "checkout", "-q", base_ref)
            _git(repo, "merge", "--no-ff", "-q", "-m", f"Merge {wt.branch}: Revert {title}",
                 notarised.sha)  # fmt: skip
            merged = _git(repo, "rev-parse", "HEAD").strip()
            return RevertRecord(True, merge_sha=merged, pr_url=f"local://{branch}", **common)
        return self._ship(repo, notarised.sha, branch, base_ref, title, why, gates, common)

    def _gates(self, target: Target, sha: str, cases: Sequence[tuple[int, str]]) -> dict[str, Any]:
        with clean_checkout(target.repo, sha) as checkout:
            g1_result = g1.build_static(target, checkout)
            gates: dict[str, Any] = {"G1": {"passed": g1_result.passed,
                                            "summary": g1_result.summary}}  # fmt: skip
            gates["G2"] = regression_check(target, checkout, cases)
        return gates

    def _ship(
        self,
        repo: Path,
        sha: str,
        branch: str,
        base_ref: str,
        title: str,
        why: str,
        gates: Mapping[str, Any],
        common: dict[str, Any],
    ) -> RevertRecord:
        cfg = self.config
        gh = self.github
        _git(repo, "push", cfg.remote, f"{sha}:refs/heads/{branch}")
        g2 = gates["G2"]
        body = (
            f"## Kintsugi auto-revert\n\n{why}\n\n"
            f"- Reverts `{common['reverted']}` (`git revert -m 1`), committed by an attested "
            f"notary session (checkpoint `{common['checkpoint'] or 'n/a'}`).\n"
            f"- Expedited gates: G1 {'✓' if gates['G1']['passed'] else '✗'}; G2 against the "
            f"pre-vaccination behaviour: {g2['cases']} canary failures replayed, "
            f"{'all pass' if g2['passed'] else 'still failing'}.\n"
        )
        pr = gh.pr_find(branch) or gh.pr_create(base_ref, branch, f"Revert {title}"[:120], body)
        number, url = int(pr["number"]), str(pr.get("url", ""))
        if cfg.require_g7:
            ok, link = self._wait_g7(number)
            common["gates"] = {**gates, "G7": {"passed": ok, "link": link}}
            if not ok:
                return RevertRecord(False, pr_url=url, reason="G7 failed", **common)
        merged = gh.pr_merge(number, cfg.merge_method)
        if merged is None:
            return RevertRecord(False, pr_url=url, reason="the merge was refused", **common)
        return RevertRecord(True, pr_url=url, merge_sha=merged, **common)

    def _wait_g7(self, number: int) -> tuple[bool, str]:
        deadline = time.monotonic() + self.config.g7_timeout_s
        while True:
            checks = [k for k in self.github.pr_checks(number)
                      if self.config.g7_check in str(k.get("name"))
                      and str(k.get("bucket")) != "skipping"]  # fmt: skip
            done = [k for k in checks if str(k.get("bucket")) in ("pass", "fail", "cancel")]
            if checks and len(done) == len(checks):
                ok = all(str(k.get("bucket")) == "pass" for k in checks)
                return ok, str(checks[0].get("link") or "")
            if time.monotonic() > deadline:
                return False, "G7 timed out"
            self.sleep(self.config.poll_s)

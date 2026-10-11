"""The Stage 6 Ledger rehearsal: one primary response end to end on a disposable repository.

A *rehearsal* is an unsealed, clearly labelled practice run (plan §4). Nothing here touches the
fleet's hosts or seals anything: the target is the **Ledger practice** repository, copied from
``tests/fixtures/rehearsal/ledger_practice`` into a scratch directory outside every repository,
and its regression is the disclosed practice refactor (``tests/fixtures/rehearsal/refactor``),
built on the ``ledger_before`` fixture shape the Architect approved for Stage 6.

1. **Prepare**: the healthy practice repository, Entire enabled (with Omega's redaction rules),
   its holdout sealed to the rehearsal recipient (``.kintsugi/holdout.pub``).
2. **Deliver** the regression the way §6.2 step 3 does: a headless Claude Code session applies
   the "readability refactor" patch, and the orchestrator commits it attested, so Entire records
   a checkpoint whose transcript states the intent ("no behavior change").
3. **Live traffic**: the host's tasks at the live seeds under the background chaos profile; every
   failed task is an observation and an R-007 hit in the senses store.
4. **Respond**: on the Temporal CLI dev server (ruling D6), the FleetSupervisor receives the hits,
   the Sentinel pages, and the Incident runs the real crew: Pathologist (G0), Archivist (intent
   record), governor and Coordinator, three headless Claude Code Surgeons in parallel
   worktrees, the blind Auditor (G0–G6), disputes, rewind, ship and the canary.
5. **Ship** through GitHub when ``github`` is set: the repository is pushed as
   ``rehearsal/<run>/main`` under a ruleset that requires a pull request and the G7 check (spike
   N9); the PR's own workflow runs ``g7-gates.yml``; the winner merges with a merge commit.
   Otherwise G7 runs locally and the merge is local.
6. **Evidence**: an aggregate-only summary (``summarize``): no holdout content, no secrets.
"""

from __future__ import annotations

import asyncio
import difflib
import shutil
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from kintsugi.control import contracts as c
from kintsugi.control.activities import CREW, SENSES, SURGEON, OmegaActivities, SqliteLedger
from kintsugi.control.ship import GhCli, GitHubShipper, LocalShipper, ShipConfig
from kintsugi.control.workflows import WORKFLOWS, FleetSupervisor
from kintsugi.crew import claude_session, templates
from kintsugi.crew.context import CrewContext, LocalTraces
from kintsugi.crew.factory import FileLedger, OmegaCrew
from kintsugi.crew.llm import ClaudeCliModel
from kintsugi.crew.surgeon import ClaudeSurgeon, session_env
from kintsugi.gates import holdout
from kintsugi.gates.checkout import clean_checkout
from kintsugi.gates.evals import run_tasks
from kintsugi.gates.protected_paths import load_policy
from kintsugi.gates.target import Target, load_target
from kintsugi.memory import entire
from kintsugi.proof import attest, passphrase, redaction
from kintsugi.proof.commit import orchestrator_commit, prompt_digest, tool_edited_paths
from kintsugi.proof.ledger import genesis_hash
from kintsugi.sandbox import proc
from kintsugi.senses.detections import Binding
from kintsugi.senses.signature import FailureObservation, from_chaos_log, tool_names
from kintsugi.senses.store import HitEvent, SenseStore, hit_key

OMEGA = Path(__file__).resolve().parents[2]
FIXTURES = OMEGA / "tests" / "fixtures" / "rehearsal"
HOST, PACKAGE = "ledger-practice", "practice_ledger"
RECONCILE = "practice_ledger/reconcile.py"
DETECTION = "omega:R-007:ledger-practice:v1"
# The spec's own framing (§6.2 step 3). A framing that also promises "keep behavior identical"
# is refused by the model, which reads the patch and sees it changes behavior (amendment A-52).
FRAMING = (
    "Refactor `reconcile`'s retry logic for readability: name the retried pass and document it."
)
RULESET_NAME = "kintsugi-rehearsal-main"
G7_CONTEXT = "g7 / G7 · ledger-practice"
IDENTITY = {"user.name": "Surya Prasanth R", "user.email": "suryaprasanthcse@gmail.com"}


class RehearsalError(RuntimeError):
    pass


@dataclass
class Rehearsal:
    run: str
    root: Path  # outside every repository
    identity: Path  # the rehearsal holdout identity (read only by the Auditor's G4 and G7)
    github: str | None = None  # owner/name: ship through pull requests and Actions G7
    token: str | None = None
    crew_model: str = "claude-cli"  # or "offline" (deterministic fallbacks only)
    models: Mapping[str, str] = field(default_factory=dict)  # role → model override
    live_seeds: tuple[int, ...] = (1, 2)
    delivery_model: str = "claude-haiku-5-5"
    log: Callable[[str], None] = print

    @property
    def repo(self) -> Path:
        return self.root / "repo"


def _git(repo: Path, *args: str, timeout_s: float = 300.0) -> str:
    return proc.run(["git", "-C", str(repo), *args], timeout_s=timeout_s, check=True).stdout


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


def refactor_diff() -> str:
    healthy = (FIXTURES / "ledger_practice" / RECONCILE).read_text(encoding="utf-8")
    refactored = (FIXTURES / "refactor" / "reconcile.py").read_text(encoding="utf-8")
    lines = difflib.unified_diff(
        healthy.splitlines(keepends=True),
        refactored.splitlines(keepends=True),
        fromfile=f"a/{RECONCILE}",
        tofile=f"b/{RECONCILE}",
    )
    return "".join(lines)


def delivery_prompt(nonce: str) -> templates.Rendered:
    return templates.render(
        "rehearsal_delivery", framing=FRAMING, nonce=nonce, path=RECONCILE, diff=refactor_diff()
    )


# ---- 1. prepare ---------------------------------------------------------------------------------


def prepare(cfg: Rehearsal) -> Target:
    if cfg.repo.exists():
        raise RehearsalError(f"{cfg.repo} exists: every rehearsal starts from a fresh repository")
    shutil.copytree(
        FIXTURES / "ledger_practice", cfg.repo, ignore=shutil.ignore_patterns("__pycache__")
    )
    _git(cfg.repo, "init", "-q", "-b", "main")
    for key, value in {**IDENTITY, "core.autocrlf": "false"}.items():
        _git(cfg.repo, "config", key, value)
    _commit(cfg.repo, "practice: the healthy ledger")
    proc.run(
        ["entire", "enable", "--agent", "claude-code", "--telemetry=false", "--skip-push-sessions"],
        cwd=cfg.repo,
        timeout_s=300,
        check=True,
    )
    redaction.install_entire_rules(cfg.repo)
    _commit(cfg.repo, "practice: enable Entire with Omega's redaction rules")
    target = load_target(cfg.repo, HOST)
    target.make_importable()
    recipient = (OMEGA / holdout.RECIPIENT).read_text(encoding="ascii").strip()
    holdout.seal(cfg.repo, HOST, PACKAGE, cfg.repo, recipient=recipient)
    _commit(cfg.repo, "practice: seal the holdout to the rehearsal recipient")
    return target


# ---- 2. deliver ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Delivery:
    commit: str
    checkpoint: str | None
    prompt_sha: str
    session: str
    cost_usd: float


def deliver(cfg: Rehearsal, signer: attest.Signer) -> Delivery:
    nonce = passphrase.new_nonce()
    rendered = delivery_prompt(nonce)
    base = _git(cfg.repo, "rev-parse", "HEAD").strip()
    started = datetime.now(UTC).isoformat(timespec="seconds")
    request = claude_session.SessionRequest(
        prompt=rendered.prompt,
        cwd=cfg.repo,
        allowed_tools=("Read", "Edit", "Bash(python -m pytest:*)"),
        model=cfg.delivery_model,
        max_budget_usd=1.5,
        timeout_s=900,
        extra_env=session_env(cfg.repo),
        append_system_prompt=rendered.system,
    )
    result = claude_session.run(request)
    if not result.ok:
        raise RehearsalError(f"the delivery session failed: {result.stream.error}")
    attestation = attest.SessionAttestation(
        nonce=nonce,
        role="rehearsal",
        incident=f"REH-{cfg.run}",
        worktree=".",
        base_sha=base,
        prompt_template_sha=prompt_digest(rendered.prompt),
        t_start=started,
        hypothesis="deliver",
    )
    outcome = orchestrator_commit(
        cfg.repo,
        attestation=attestation,
        signer=signer,
        subject="refactor(reconcile): name the retried pass and document it",
        body=FRAMING,
        policy=load_policy(),
        session_paths=tool_edited_paths(result.stream.tool_uses, cfg.repo),
    )
    return Delivery(
        outcome.sha,
        outcome.checkpoint_id,
        rendered.prompt_sha,
        result.session_id,
        result.stream.cost_usd,
    )


# ---- 3. live traffic and hits -------------------------------------------------------------------


def live_traffic(cfg: Rehearsal, target: Target) -> list[FailureObservation]:
    import importlib

    train = importlib.import_module(f"{PACKAGE}.evals.generate").tasks("train")
    found: list[FailureObservation] = []
    with clean_checkout(cfg.repo, "main") as checkout:
        for seed in cfg.live_seeds:
            run = run_tasks(target, checkout, train, seed=seed, phase="rehearsal")
            found += from_chaos_log(
                run.chaos_log,
                run.verdicts(),
                host=HOST,
                split="train",
                seed=seed,
                run=f"{cfg.run}-s{seed}",
                phase="rehearsal",
                services=tool_names(target.host.tools),
                detection_ids=[DETECTION],
            )
    LocalTraces.write(cfg.root / "observations", f"live-{cfg.run}", found)
    return found


def claim_hits(store: SenseStore, observations: list[FailureObservation]) -> list[c.Hit]:
    start = datetime.now(UTC) - timedelta(seconds=len(observations))
    hits = []
    for n, obs in enumerate(observations):
        at = (start + timedelta(seconds=n)).isoformat().replace("+00:00", "Z")
        key = hit_key(DETECTION, obs.trace_id, "verdict")
        store.claim_hit(
            HitEvent(
                key,
                DETECTION,
                obs.trace_id,
                "verdict",
                "poller",
                detected_at=at,
                receptor="R-007",
                host=HOST,
            )
        )
        hits.append(c.Hit(key, DETECTION, obs.trace_id, "verdict", "poller", "R-007", HOST, at))
    return hits


# ---- GitHub (spike N9) --------------------------------------------------------------------------


def ruleset_body(include: list[str]) -> dict[str, Any]:
    """Explicit branches, added as each run creates its base: a required status check also
    blocks the push that *creates* a matching branch (spike N9), so a glob would make every new
    rehearsal base unpushable."""
    return {
        "name": RULESET_NAME,
        "target": "branch",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": sorted(set(include)), "exclude": []}},
        "rules": [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {
                "type": "pull_request",
                "parameters": {
                    "required_approving_review_count": 0,
                    "dismiss_stale_reviews_on_push": False,
                    "require_code_owner_review": False,
                    "require_last_push_approval": False,
                    "required_review_thread_resolution": False,
                },
            },
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": False,
                    "required_status_checks": [{"context": G7_CONTEXT}],
                },
            },
        ],
        "bypass_actors": [],
    }


def github_setup(cfg: Rehearsal, gh: GhCli) -> dict[str, Any]:
    """Push the practice repository as ``rehearsal/<run>/main`` under the rehearsal ruleset and
    show that a direct push to it is refused (N9)."""
    assert cfg.github is not None
    rulesets = gh.api("GET", f"repos/{cfg.github}/rulesets") or []
    existing = next((r for r in rulesets if r.get("name") == RULESET_NAME), None)
    include: list[str] = []
    if existing is not None:
        current = gh.api("GET", f"repos/{cfg.github}/rulesets/{existing['id']}") or {}
        found = ((current.get("conditions") or {}).get("ref_name") or {}).get("include") or []
        include = [i for i in found if "*" not in i]
        if include != list(found):  # a glob would also block creating the new base (N9)
            gh.api(
                "PUT",
                f"repos/{cfg.github}/rulesets/{existing['id']}",
                ruleset_body(include or ["refs/heads/rehearsal/none/main"]),
            )
    _git(cfg.repo, "remote", "add", "origin", f"https://github.com/{cfg.github}.git")
    base = f"rehearsal/{cfg.run}/main"
    refs = sorted(entire.local_checkpoint_refs(cfg.repo))
    if refs:
        redaction.assert_pushable(cfg.repo, refs)
    _git(cfg.repo, "push", "origin", f"main:refs/heads/{base}", *[f"{r}:{r}" for r in refs])
    # Protection starts the moment the base exists: add exactly this branch to the ruleset.
    ref = f"refs/heads/{base}"
    if existing is None:
        ruleset = gh.api("POST", f"repos/{cfg.github}/rulesets", ruleset_body([ref]))
    else:
        ruleset = gh.api(
            "PUT", f"repos/{cfg.github}/rulesets/{existing['id']}", ruleset_body([*include, ref])
        )
    probe = _git(cfg.repo, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "N9 probe").strip()
    refused = proc.run(
        ["git", "-C", str(cfg.repo), "push", "origin", f"{probe}:refs/heads/{base}"], timeout_s=120
    )
    return {
        "base": base,
        "ruleset_id": ruleset.get("id"),
        "ruleset": RULESET_NAME,
        "direct_push_refused": not refused.ok,
        "direct_push_message": refused.stderr.strip().splitlines()[-3:],
    }


# ---- 4. respond ---------------------------------------------------------------------------------


async def respond(
    crew: OmegaCrew,
    hits: list[c.Hit],
    run: str,
    config: c.ControlConfig,
    log: Callable[[str], None],
) -> tuple[str, c.Outcome]:
    async with await WorkflowEnvironment.start_local() as env:
        acts = OmegaActivities(crew, SqliteLedger(crew.ctx.state / "steps.sqlite"))
        async with (
            Worker(
                env.client,
                task_queue=CREW,
                workflows=list(WORKFLOWS),
                activities=acts.activities_for(CREW),
                max_concurrent_activities=8,
            ),
            Worker(
                env.client,
                task_queue=SURGEON,
                activities=acts.activities_for(SURGEON),
                max_concurrent_activities=3,
            ),
            Worker(env.client, task_queue=SENSES, activities=acts.activities_for(SENSES)),
        ):
            supervisor_id = f"fleet-supervisor-{run}"
            sup = await env.client.start_workflow(
                FleetSupervisor.run, args=[config, None], id=supervisor_id, task_queue=CREW
            )
            for hit in hits:
                await sup.signal(FleetSupervisor.hit, hit)
            incident = None
            while incident is None:
                await asyncio.sleep(2)
                status = await sup.query(FleetSupervisor.status)
                opened = await sup.query(FleetSupervisor.open_incidents)
                incident = next(iter(sorted(opened)), None) or next(
                    iter(sorted(status["closed"])), None
                )
            log(f"incident {incident} opened by the Sentinel")
            outcome = await env.client.get_workflow_handle(incident, result_type=c.Outcome).result()
            await sup.terminate("rehearsal over")
            return incident, outcome


def run(cfg: Rehearsal) -> dict[str, Any]:
    cfg.root.mkdir(parents=True, exist_ok=True)
    keys = cfg.root / "keys"
    keys.mkdir(exist_ok=True)
    signer = attest.Ed25519Signer.generate()  # a rehearsal key, never the T0 key (A-4)
    signer.save(keys / "attest-rehearsal", keys / "attest-rehearsal.pub")
    cfg.log(f"[{cfg.run}] preparing {cfg.repo}")
    target = prepare(cfg)
    cfg.log(f"[{cfg.run}] delivering the practice regression through a headless session")
    delivery = deliver(cfg, signer)
    cfg.log(f"[{cfg.run}] delivered {delivery.commit[:12]} (checkpoint {delivery.checkpoint})")
    observations = live_traffic(cfg, target)
    cfg.log(f"[{cfg.run}] live traffic: {len(observations)} failed tasks")
    if not observations:
        raise RehearsalError("the delivered change produced no failure: nothing to repair")
    github: dict[str, Any] = {}
    gh = GhCli(cfg.github, token=cfg.token) if cfg.github else None
    if gh is not None:
        github = github_setup(cfg, gh)
        cfg.log(f"[{cfg.run}] GitHub: {github}")
    store = SenseStore(cfg.root / "senses.sqlite")
    hits = claim_hits(store, observations)
    binding = Binding(DETECTION, "R-007", HOST, 1, threshold_count=3, window_minutes=10)
    model = ClaudeCliModel(overrides=dict(cfg.models)) if cfg.crew_model == "claude-cli" else None
    ctx = CrewContext(
        state=cfg.root / "crew",
        targets={HOST: target},
        model=model,
        store=store,
        bindings={DETECTION: binding},
        traces=LocalTraces(cfg.root / "observations"),
        holdout_identity=cfg.identity,
        incident_prefix="INC-REH",
        models=dict(cfg.models),
    )
    ledger = FileLedger(
        cfg.root / "ledger.jsonl", genesis_hash(f"rehearsal-{cfg.run}", signer.public_key_raw())
    )

    def head() -> tuple[int, str]:
        entries = ledger.entries()
        return (len(entries), entries[-1]["hash"]) if entries else (1, ledger.genesis)

    shipper: Any
    if gh is not None and cfg.github is not None:
        shipper = GitHubShipper(
            ctx, ShipConfig(cfg.github, branch_prefix=f"rehearsal/{cfg.run}/"), gh, ledger_head=head
        )
    else:
        shipper = LocalShipper(
            ctx, seed=int(time.time()) % 100000, identity=cfg.identity, ledger_head=head
        )
    surgeon = ClaudeSurgeon(ctx, signer)
    crew = OmegaCrew(ctx, runner=lambda: surgeon, shipper=shipper, ledger=ledger)
    config = c.ControlConfig(
        supervisor_id=f"fleet-supervisor-{cfg.run}", canary_interval_s=5.0, canary_max_batches=8
    )
    started = time.monotonic()
    incident, outcome = asyncio.run(respond(crew, hits, cfg.run, config, cfg.log))
    cfg.log(f"[{cfg.run}] {incident} ended {outcome.state}: {outcome.reason}")
    from kintsugi.crew.evidence import summarize

    return summarize(
        cfg,
        ctx,
        crew,
        signer,
        delivery,
        incident,
        outcome,
        store,
        github,
        duration_s=time.monotonic() - started,
    )

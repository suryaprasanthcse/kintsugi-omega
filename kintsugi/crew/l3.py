"""The Stage 7 rehearsal: an antibody's life, autoimmunity included (spec §6.5 steps 12–14, §6.7).

A rehearsal is an unsealed, clearly labelled practice run (plan §4) on disposable repositories
outside every repository, shipping only through ``rehearsal/<run>/*`` branches (A-57):

1. **Synthesis** (step 12): AB-1@1.0 from the Stage 6 incident that resolved
   (``reh-20261011-235459``: winner ``e50387e``), on the origin's own before/after graphs.
2. **History**: the Stage 6 rehearsals' observations (claimed by their incidents), the CRM
   practice host's baseline runs (its latent defect leaves silent duplicates) and Scout's.
3. **Maturation** (step 13), ``CrossHostMaturation`` on the Temporal dev server: the CRM practice
   host (the Clinic stand-in, A-61) fails the induced trigger, a Surgeon applies the recipe, the
   blind Auditor runs G0–G6, a pull request is opened (never merged), the receptor's precision
   on history activates the antibody; then the **backfill**.
4. **Vaccination wave** (step 14) under a ``FleetSupervisor``: the motif matches Scout, and
   Scout fails the induced trigger too (restarting every page fetch from page one exhausts its
   retries), so the rule lets the wave vaccinate it.
5. **The disclosed L3 drill**: Omega's gates would not ship the recipe's literal application to
   Scout (its replayed trigger still fails), so the drill *injects* that wrong vaccination: a
   notary session delivers it, the Auditor's verdict is recorded as it is, and it merges anyway.
   Everything after the merge is Omega's own response, unscripted: Scout's canary (SPRT against
   Scout's baseline), the revert pull request and its reverse canary, ``pause_antibody``,
   QUARANTINED with the receptor kept, the notices on open vaccination PRs, trace-to-test,
   contrastive learning (v1.1 excludes Scout), re-maturation, and the wave's resumption.
6. **Evidence**: an aggregate summary (no holdout content, no secrets).
"""

from __future__ import annotations

import asyncio
import difflib
import shutil
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from kintsugi.control import contracts as c
from kintsugi.control.activities import CREW, SENSES, SURGEON, OmegaActivities, SqliteLedger
from kintsugi.control.revert import RevertConfig, Reverter
from kintsugi.control.ship import GhCli, GitHubShipper, LocalShipper, ShipConfig
from kintsugi.control.workflows import (
    WORKFLOWS,
    CrossHostMaturation,
    FleetSupervisor,
    VaccinationWave,
)
from kintsugi.crew.context import CrewContext
from kintsugi.crew.factory import FileLedger, Immunity, OmegaCrew
from kintsugi.crew.immunologist import History, Immunologist, Record
from kintsugi.crew.llm import ClaudeCliModel
from kintsugi.crew.notary import Notary, NotaryRequest
from kintsugi.crew.surgeon import ClaudeSurgeon
from kintsugi.gates import holdout
from kintsugi.gates.checkout import clean_checkout
from kintsugi.gates.evals import run_tasks
from kintsugi.gates.target import Target, load_target
from kintsugi.memory import entire, graph, predicates
from kintsugi.memory.antibody import Antibody
from kintsugi.memory.library import AntibodyLibrary
from kintsugi.proof import attest, redaction
from kintsugi.proof.ledger import genesis_hash
from kintsugi.sandbox import proc, worktree

OMEGA = Path(__file__).resolve().parents[2]
GRAPH_FIXTURES = OMEGA / "tests" / "fixtures" / "graph"
REHEARSALS = Path.home() / ".kintsugi" / "rehearsals"
ORIGIN_RUN = "reh-20261011-235459"
ORIGIN_INCIDENT = "INC-R235459-0001"
ORIGIN_WINNER = "H2"
IDENTITY = {"user.name": "Surya Prasanth R", "user.email": "suryaprasanthcse@gmail.com"}
SCOUT_TARGET = """# How KINTSUGI-Omega's gates check this repository (a disposable Scout copy).
version: 1
hosts:
  scout:
    host_yaml: host.yaml
    package_root: "."
    tests: tests
    lint: [scout]
    typecheck: [scout]
    holdout: scout/evals/holdout/tasks.jsonl.age
    stress_profile: profiles/stress.yaml
    live_services: []
"""
SCOUT_CHECKS = """
# The fleet's lint and type-check settings (the monorepo's root configuration), so G1 judges
# this copy exactly as it judges hosts/scout.
[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["B", "C4", "E", "F", "I", "PIE", "PT", "RUF", "SIM", "TID", "UP", "W"]
allowed-confusables = ["×", "‖", "–", "—", "−", "′", "″"]

[tool.ruff.lint.isort]
known-first-party = ["scout", "hostkit"]

[tool.pyright]
include = ["scout"]
pythonVersion = "3.12"
typeCheckingMode = "standard"
"""
NESTED_BUFFER = "    def attempt() -> list[Passage]:\n        buffer = ScratchBuffer()\n"
HOISTED_BUFFER = (
    "    buffer = ScratchBuffer()  # outside the retried scope (AB-1@1.0, applied literally)\n\n"
    "    def attempt() -> list[Passage]:\n"
)


class L3Error(RuntimeError):
    pass


@dataclass
class L3Rehearsal:
    run: str
    root: Path  # outside every repository
    identity: Path  # the rehearsal holdout identity
    library: Path = OMEGA / "antibodies"
    github: str | None = None  # owner/name: pull requests on rehearsal/<run>/* branches
    token: str | None = None
    crew_model: str = "claude-cli"
    log: Callable[[str], None] = print
    timings: dict[str, float] = field(default_factory=dict)


def _git(repo: Path, *args: str, timeout_s: float = 300.0) -> str:
    return proc.run(["git", "-C", str(repo), *args], timeout_s=timeout_s, check=True).stdout


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


def _init(repo: Path, message: str) -> str:
    _git(repo, "init", "-q", "-b", "main")
    for key, value in {**IDENTITY, "core.autocrlf": "false"}.items():
        _git(repo, "config", key, value)
    _commit(repo, message)
    proc.run(
        ["entire", "enable", "--agent", "claude-code", "--telemetry=false", "--skip-push-sessions"],
        cwd=repo,
        timeout_s=300,
        check=True,
    )
    redaction.install_entire_rules(repo)
    return _commit(repo, "practice: enable Entire with Omega's redaction rules")


# ---- 1. the disposable hosts ---------------------------------------------------------------------


def prepare_crm(cfg: L3Rehearsal) -> Target:
    repo = cfg.root / "repos" / "crm"
    shutil.copytree(
        OMEGA / "tests" / "fixtures" / "rehearsal" / "crm_practice",
        repo,
        ignore=shutil.ignore_patterns("__pycache__", ".github"),
    )
    _init(repo, "practice: the CRM sync host (disclosed latent defect)")
    target = load_target(repo, "crm-practice")
    target.make_importable()
    recipient = (OMEGA / holdout.RECIPIENT).read_text(encoding="ascii").strip()
    holdout.seal(repo, "crm-practice", "practice_crm", repo, recipient=recipient)
    _commit(repo, "practice: seal the holdout to the rehearsal recipient")
    return target


def prepare_scout(cfg: L3Rehearsal) -> Target:
    repo = cfg.root / "repos" / "scout"
    shutil.copytree(
        OMEGA / "hosts" / "scout",
        repo,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"),
    )
    (repo / ".kintsugi").mkdir()
    (repo / ".kintsugi" / "target.yaml").write_text(SCOUT_TARGET, encoding="utf-8", newline="\n")
    (repo / "profiles").mkdir()
    shutil.copy2(
        OMEGA / "kintsugi" / "sandbox" / "profiles" / "stress.yaml",
        repo / "profiles" / "stress.yaml",
    )
    (repo / ".gitignore").write_text(
        "__pycache__/\n*.pyc\n.pytest_cache/\n", encoding="utf-8", newline="\n"
    )
    with open(repo / "pyproject.toml", "a", encoding="utf-8", newline="\n") as handle:
        handle.write(SCOUT_CHECKS)  # in the monorepo, the root configuration applies
    _init(repo, "practice: a disposable copy of the Scout host")
    return load_target(repo, "scout")


def publish(cfg: L3Rehearsal, gh: GhCli, repo: Path, host: str) -> str:
    """Push a practice repository's main as ``rehearsal/<run>/<host>/main`` (no ruleset: the
    drill's branches are unprotected, disclosed)."""
    base = f"rehearsal/{cfg.run}/{host}/main"
    _git(repo, "remote", "add", "origin", f"https://github.com/{cfg.github}.git")
    refs = sorted(entire.local_checkpoint_refs(repo))
    if refs:
        redaction.assert_pushable(repo, refs)
    _git(repo, "push", "-q", "origin", f"main:refs/heads/{base}", *[f"{r}:{r}" for r in refs])
    return base


# ---- 2. history ---------------------------------------------------------------------------------


def record_history(cfg: L3Rehearsal, targets: Mapping[str, Target]) -> History:
    """Recorded runs the receptor and the backfill read (Omega-side, A-29)."""
    import importlib
    import json

    out = cfg.root / "history"
    claimed: dict[str, str] = {}
    folders = sorted(
        p / "observations" for p in REHEARSALS.glob("reh-*") if (p / "observations").is_dir()
    )
    for folder in folders:
        for file in folder.glob("*.jsonl"):
            for line in file.read_text(encoding="utf-8").splitlines():
                if line.strip():  # every Stage 6 failure paged its run's incident
                    claimed[str(json.loads(line)["trace_id"])] = f"INC-{folder.parent.name}"
    for host, seeds in (("crm-practice", (1, 2)), ("scout", (1,))):
        target = targets[host]
        target.make_importable()
        tasks = importlib.import_module(f"{target.host.package}.evals.generate").tasks("train")
        with clean_checkout(target.repo, "main") as checkout:
            for seed in seeds:
                runs = run_tasks(target, checkout, tasks, seed=seed, phase="baseline").runs
                History.write(
                    out / f"{host}-baseline.jsonl",
                    [
                        Record(
                            f"local:{host}-baseline-s{seed}:{r.task}",
                            host,
                            r.verdict,
                            f"{host} baseline seed {seed}",
                        )
                        for r in runs
                    ],
                )
    return History.load(*folders, out, claimed=claimed)


# ---- 3. the disclosed drill: an injected wrong vaccination --------------------------------------


def literal_recipe_diff(repo: Path) -> str:
    path = "scout/research.py"
    before = (repo / path).read_text(encoding="utf-8")
    if NESTED_BUFFER not in before:
        raise L3Error("Scout's retried scope no longer constructs its ScratchBuffer")
    after = before.replace(NESTED_BUFFER, HOISTED_BUFFER)
    lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    )
    return "".join(lines)


def drill_applier(notary: Notary, reverter: Reverter, log: Callable[[str], None]) -> Any:
    def apply(crew: OmegaCrew, step: Any, ab: Antibody, host: str) -> c.VaccinationResult:
        assert crew.immunity is not None
        imm = crew.immunity.immunologist
        trigger = imm.induced_trigger(ab, host)
        imm.library.record(ab.id, {"event": "induced-trigger", **trigger.as_dict()})
        log(
            f"[drill] induced trigger on {host}: produced_failure={trigger.produced_failure} "
            f"{dict(trigger.classes)}"
        )
        if not trigger.produced_failure:
            return c.VaccinationResult(
                ab.id, host, False, "the induced trigger produced no failure"
            )
        case, card = imm.prepare(ab, host, trigger)
        target = crew.ctx.target(host)
        wt = worktree.create(target.repo, card.incident_id, card.hypothesis, base=card.base_sha)
        delivered = notary.commit(
            wt.path,
            NotaryRequest(
                operation="drill-deliver",
                reference=f"{ab.ref} on {host} (disclosed L3 drill)",
                incident=card.incident_id,
                purpose=(
                    f"Disclosed L3 drill: deliver {ab.ref}'s recipe to {host} exactly as the "
                    "antibody states it (ScratchBuffer constructed once, outside the retried "
                    "scope `attempt`), so that Omega's canary, revert and quarantine are "
                    "exercised on a wrong vaccination. Apply the patch exactly as written."
                ),
                diff=literal_recipe_diff(target.repo),
                subject=f"fix({card.incident_id}): Vaccinate {host} with {ab.ref} [L3 drill]",
                body="Disclosed L3 drill: the recipe applied literally, injected past the gates.",
            ),
            package_root=target.package_root,
        )
        out = c.SurgeonOutcome(
            card.incident_id,
            card.hypothesis,
            1,
            wt.branch,
            delivered.nonce,
            delivered.sha,
            delivered.checkpoint_id,
        )
        verdict = crew.auditor.audit(out)  # recorded as it is; the drill merges regardless
        log(
            f"[drill] Auditor on the injected vaccination: passed={verdict.passed} "
            f"({verdict.reason[:200]})"
        )
        case.put(
            "antibody",
            "drill",
            {
                "injected": True,
                "passed": verdict.passed,
                "gates": verdict.gates,
                "reason": verdict.reason,
                "commit": delivered.sha,
                "checkpoint": delivered.checkpoint_id,
            },
            by="immunologist",
        )
        shipped = reverter.ship_commit(
            target.repo,
            delivered.sha,
            wt.branch,
            f"[L3 drill] Vaccinate {host} with {ab.ref} (injected wrong fix)",
            (
                f"Disclosed L3 drill (spec §6.7): {ab.ref}'s recipe applied literally to {host}, "
                f"merged past the gates (the Auditor's verdict: passed={verdict.passed}; "
                f"{verdict.reason[:300]}). Omega's canary decides what happens next."
            ),
        )
        if not shipped["merge_sha"]:
            raise L3Error(f"the drill's merge failed: {shipped['reason']}")
        imm.library.protect(ab.id, host)
        shipment = c.Shipment(
            card.incident_id,
            ab.ref,
            shipped["branch"],
            shipped["pr_url"],
            False,
            True,
            shipped["merge_sha"],
        )
        crew.ledger.append(
            {
                "type": "vaccination",
                "antibody": ab.ref,
                "host": host,
                "drill": "l3-injected-wrong-fix",
                "pr": shipped["pr_url"],
                "auditor_passed": verdict.passed,
            }
        )
        return c.VaccinationResult(ab.id, host, True, "L3 drill: injected, merged", shipment)

    return apply


# ---- 4. the control plane -----------------------------------------------------------------------


async def respond(
    crew: OmegaCrew,
    ab: c.AntibodyRef,
    origin: str,
    config: c.ControlConfig,
    log: Callable[[str], None],
) -> dict[str, Any]:
    async with await WorkflowEnvironment.start_local() as env:
        acts = OmegaActivities(crew, SqliteLedger(crew.ctx.state / "steps.sqlite"))
        async with (
            Worker(
                env.client,
                task_queue=CREW,
                workflows=list(WORKFLOWS),
                activities=acts.activities_for(CREW),
                max_concurrent_activities=4,
            ),
            Worker(
                env.client,
                task_queue=SURGEON,
                activities=acts.activities_for(SURGEON),
                max_concurrent_activities=2,
            ),
            Worker(env.client, task_queue=SENSES, activities=acts.activities_for(SENSES)),
        ):
            sup = await env.client.start_workflow(
                FleetSupervisor.run, args=[config, None], id=config.supervisor_id, task_queue=CREW
            )
            log(f"maturation of {ab.antibody_id}@{ab.version} (CrossHostMaturation)")
            matured = await env.client.execute_workflow(
                CrossHostMaturation.run,
                args=[ab, origin, config],
                id=f"mature-{ab.antibody_id}",
                task_queue=CREW,
            )
            log(f"maturation: {matured}")
            if not matured.ok:
                await sup.terminate("maturation failed")
                return {"maturation": matured, "wave": None, "episodes": [], "supervisor": None}
            assert crew.immunity is not None
            imm = crew.immunity.immunologist
            backfill = imm.backfill(imm.library.load(ab.antibody_id))
            log(
                f"backfill: {backfill['silent']} silent past occurrences, ${backfill['impact_usd']}"
            )
            active = c.AntibodyRef(ab.antibody_id, str(matured.detail.get("version")), "ACTIVE")
            log(f"vaccination wave with {active.antibody_id}@{active.version}")
            wave = await env.client.start_workflow(
                VaccinationWave.run,
                args=[active, config],
                id=f"vaccinate-{ab.antibody_id}",
                task_queue=CREW,
            )
            results = await wave.result()
            episodes = await wave.query(VaccinationWave.autoimmunity)
            status = await sup.query(FleetSupervisor.status)
            await sup.terminate("rehearsal over")
            return {
                "maturation": matured,
                "backfill": backfill,
                "wave": results,
                "episodes": episodes,
                "supervisor": status,
            }


def run(cfg: L3Rehearsal) -> dict[str, Any]:
    started = time.monotonic()
    cfg.root.mkdir(parents=True, exist_ok=True)
    origin = REHEARSALS / ORIGIN_RUN
    if not (origin / "crew" / "cases" / ORIGIN_INCIDENT).is_dir():
        raise L3Error(f"the resolved Stage 6 incident is missing under {origin}")
    keys = cfg.root / "keys"
    keys.mkdir(exist_ok=True)
    signer = attest.Ed25519Signer.generate()  # a rehearsal key, never the T0 key (A-4)
    signer.save(keys / "attest-rehearsal", keys / "attest-rehearsal.pub")
    state = cfg.root / "crew"
    shutil.copytree(origin / "crew" / "cases" / ORIGIN_INCIDENT, state / "cases" / ORIGIN_INCIDENT)
    cfg.log(f"[{cfg.run}] preparing the CRM practice host and a Scout copy")
    crm, scout = prepare_crm(cfg), prepare_scout(cfg)
    ledger_target = load_target(origin / "repo", "ledger-practice")
    targets = {"ledger-practice": ledger_target, "crm-practice": crm, "scout": scout}
    gh = GhCli(cfg.github, token=cfg.token) if cfg.github else None
    prefixes = (f"rehearsal/{cfg.run}/crm/", f"rehearsal/{cfg.run}/scout/")
    if gh is not None:
        for host, target in (("crm", crm), ("scout", scout)):
            cfg.log(f"[{cfg.run}] published {publish(cfg, gh, target.repo, host)}")
    cfg.log(f"[{cfg.run}] recording history (Stage 6 observations, CRM and Scout baselines)")
    history = record_history(cfg, targets)
    model = ClaudeCliModel() if cfg.crew_model == "claude-cli" else None
    ctx = CrewContext(
        state=state,
        targets=targets,
        model=model,
        holdout_identity=cfg.identity,
        incident_prefix="INC-L3",
    )
    ledger = FileLedger(
        cfg.root / "ledger.jsonl", genesis_hash(f"rehearsal-{cfg.run}", signer.public_key_raw())
    )
    library = AntibodyLibrary(cfg.library)
    imm = Immunologist(
        ctx, library, history=history, ledger=ledger, positives={"ledger@pre-fix": ledger_pre_fix()}
    )
    notary = Notary(signer)
    if gh is not None:
        crm_ship: Any = GitHubShipper(
            ctx,
            ShipConfig(cfg.github or "", branch_prefix=prefixes[0], pr_only=True),
            gh,
            ledger_head=lambda: _head(ledger),
        )
        reverter = Reverter(
            notary, RevertConfig(branch_prefix=prefixes[1], require_g7=False), github=gh
        )
    else:
        crm_ship = LocalShipper(
            ctx, seed=4242, identity=cfg.identity, ledger_head=lambda: _head(ledger)
        )
        reverter = Reverter(notary, RevertConfig(require_g7=False))
    surgeon = ClaudeSurgeon(ctx, signer)
    immunity = Immunity(
        imm,
        shippers={"crm-practice": crm_ship},
        reverters={"scout": reverter},
        notary=notary,
        github=gh,
        pr_prefixes=prefixes,
        appliers={"scout": drill_applier(notary, reverter, cfg.log)},
    )
    crew = OmegaCrew(ctx, runner=lambda: surgeon, ledger=ledger, immunity=immunity)

    cfg.log(f"[{cfg.run}] synthesising the antibody from {ORIGIN_INCIDENT}")
    winner = ctx.case(ORIGIN_INCIDENT).reader("immunologist").get("outcome", ORIGIN_WINNER)
    out = c.SurgeonOutcome(
        ORIGIN_INCIDENT,
        ORIGIN_WINNER,
        1,
        f"kintsugi/{ORIGIN_INCIDENT}/H2",
        str(winner.get("nonce", "")),
        str(winner["commit"]),
        winner.get("checkpoint"),
    )
    ab = imm.synthesize(out)
    cfg.log(f"[{cfg.run}] {ab.ref} {ab.state}: binding {ab.evidence.get('binding')}")
    config = c.ControlConfig(
        supervisor_id=f"fleet-supervisor-{cfg.run}", canary_interval_s=2.0, canary_max_batches=14
    )
    outcome = asyncio.run(
        respond(
            crew,
            c.AntibodyRef(ab.id, ab.version, str(ab.state)),
            "ledger-practice",
            config,
            cfg.log,
        )
    )
    return summarize(cfg, ctx, library, ab.id, outcome, ledger, time.monotonic() - started)


def ledger_pre_fix() -> Any:
    """Ledger@pre-fix (the approved ``ledger_before`` graph fixture): a known positive for
    contrastive learning (spec §6.7 step 19 lists it first)."""
    text = (GRAPH_FIXTURES / "snapshots" / "ledger_before.ndjson").read_text(encoding="utf-8")
    return predicates.host_graph(
        graph.parse_snapshot(text.splitlines()),
        package_dir="ledger",
        side_effect_tools={"post_journal_entry"},
        source_root=GRAPH_FIXTURES / "ledger_before",
    )


def _head(ledger: FileLedger) -> tuple[int, str]:
    entries = ledger.entries()
    return (len(entries), entries[-1]["hash"]) if entries else (1, ledger.genesis)


# ---- 5. evidence --------------------------------------------------------------------------------


def summarize(
    cfg: L3Rehearsal,
    ctx: CrewContext,
    library: AntibodyLibrary,
    ab_id: str,
    outcome: Mapping[str, Any],
    ledger: FileLedger,
    duration_s: float,
) -> dict[str, Any]:
    """Aggregates only: states, links, decisions, counts and costs (no holdout content)."""
    ab = library.load(ab_id)
    cases = sorted(p.name for p in (ctx.state / "cases").iterdir() if p.is_dir())
    costs = {name: ctx.case(name).spent() for name in cases}
    origin_cost = costs.get(ORIGIN_INCIDENT, 0.0)
    vaccinations = {k: v for k, v in costs.items() if k.startswith("VAC-")}
    lineage = library.lineage(ab_id)
    wave = outcome.get("wave") or []
    return {
        "run": cfg.run,
        "kind": "stage7-l3-rehearsal (disclosed drill: the Scout vaccination is injected)",
        "antibody": {
            "ref": ab.ref,
            "state": str(ab.state),
            "versions": ab.lineage.versions,
            "quarantines": ab.lineage.quarantines,
            "hosts_protected": ab.lineage.hosts_protected,
            "preconditions": ab.preconditions,
            "receptor": ab.receptor.model_dump(),
            "motif_edges": [list(e) for e in ab.motif.edges],
        },
        "lineage_events": [
            {
                k: e.get(k)
                for k in ("event", "version", "from", "to", "host", "why", "silent", "matched")
            }
            for e in lineage.get("events", [])
        ],
        "refinements": lineage.get("refinements", []),
        "maturation": _plain(outcome.get("maturation")),
        "backfill": {
            k: v for k, v in (outcome.get("backfill") or {}).items() if k != "silent_traces"
        },
        "wave": [
            {
                "host": r.host,
                "applied": r.applied,
                "reason": r.reason,
                "pr": r.shipment.pr_url if r.shipment else None,
            }
            for r in wave
        ],
        "autoimmunity": outcome.get("episodes"),
        "supervisor": outcome.get("supervisor"),
        "costs": {
            "origin_primary_usd": origin_cost,
            "vaccinations_usd": vaccinations,
            "cases": costs,
        },
        "ledger_entries": len(ledger.entries()),
        "duration_s": round(duration_s, 1),
    }


def _plain(value: Any) -> Any:
    from kintsugi.crew.casefile import plain

    return plain(value) if value is not None else None

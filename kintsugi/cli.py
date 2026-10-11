"""`kintsugi` command-line entry point.

Commands that belong to later stages exit with code 2 and say which stage builds them.
For `audit`, code 2 is the spec's own meaning: "checks skipped" (§7.5).
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from collections.abc import Sequence
from pathlib import Path

from kintsugi import PINNED_TOOLCHAIN, __version__
from kintsugi.proof.attest import Ed25519Signer, fingerprint, load_public_key

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_SKIPPED = 2

# Command → the execution-plan stage that implements it.
_PENDING: dict[str, str] = {
    "audit": "Stage 8 (proof plane: kintsugi audit C1–C14)",
    "demo": "Stage 13 (dress rehearsal)",
    "fleet": "Stage 3 (fleet runtime) and Stage 5 (control plane)",
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kintsugi",
        description="KINTSUGI-Ω: an immune system for AI agents.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("version", help="print the version and the pinned toolchain")

    audit = sub.add_parser("audit", help="verify the autonomy window (spec §7.5)")
    audit.add_argument("--all", action="store_true", help="run every check C1–C14")
    audit.add_argument("--autonomy", action="store_true", help="checks C2–C5 and C12")
    audit.add_argument("--incident", metavar="INC", help="one incident")
    audit.add_argument("--sabotage", metavar="S", help="one sealed sabotage")
    audit.add_argument("--antibody", metavar="AB", help="one antibody lineage")
    audit.add_argument("--offline", metavar="BUNDLE_DIR", help="verify published bundles only")
    audit.add_argument("--json", action="store_true", help="machine-readable output")

    sub.add_parser("demo", help="run the recorded demo scenario")

    fleet = sub.add_parser("fleet", help="the host fleet: baselines, start, stop")
    fleet_sub = fleet.add_subparsers(dest="fleet_command", required=True)
    up = fleet_sub.add_parser("up", help="run the hosts: auto-deploy from main + live traffic")
    up.add_argument("--host", action="append", help="host id (default: every host)")
    up.add_argument("--seed", type=int, default=1)
    up.add_argument("--ref", default="origin/main", help="what the deploy worktree follows")
    up.add_argument("--no-deploy", action="store_true", help="run the installed code instead")
    up.add_argument("--scale", type=float, default=1.0, help="traffic speed-up (drills only)")
    up.add_argument("--duration", type=float, help="seconds to run (default: until fleet down)")
    fleet_sub.add_parser("down", help="ask a running `fleet up` to finish and exit")
    base = fleet_sub.add_parser("baseline", help="3-seed baseline and health card (spec §6.1)")
    base.add_argument("--host", action="append", help="host id (default: every host)")
    base.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3])
    base.add_argument("--holdout-identity", type=Path, help="age identity file (outside repo)")
    base.add_argument("--workers", type=int, default=8)
    base.add_argument("--out-dir", type=Path, default=Path("evidence/baselines"))

    holdout = sub.add_parser("holdout", help="holdout custody (spec §5.11, I5, C13)")
    holdout_sub = holdout.add_subparsers(dest="holdout_command", required=True)
    keygen = holdout_sub.add_parser("keygen", help="new age identity; commits only the recipient")
    keygen.add_argument("--identity", type=Path, required=True, help="outside any repo")
    seal = holdout_sub.add_parser("seal", help="generate, canary and encrypt a host's holdout")
    seal.add_argument("--host", action="append", help="host id (default: every host)")
    holdout_sub.add_parser("verify", help="check sealed files against .kintsugi/holdout.sha256")

    senses = sub.add_parser("senses", help="neatlogs receptors, webhook ingress, poller (§4.1)")
    senses_sub = senses.add_subparsers(dest="senses_command", required=True)
    prov = senses_sub.add_parser("provision", help="receptors R-001…R-007, alert rules, webhook")
    prov.add_argument("--public-url", help="the ingress's public origin (e.g. a tunnel)")
    prov.add_argument("--dry-run", action="store_true", help="render and validate only")
    ingress = senses_sub.add_parser("ingress", help="serve the webhook ingress")
    ingress.add_argument("--port", type=int, default=8787)
    ingress.add_argument("--host", default="127.0.0.1")
    poll = senses_sub.add_parser("poll", help="the reconciliation poller (source of truth)")
    poll.add_argument("--once", action="store_true", help="one sweep of every receptor")
    poll.add_argument("--interval", type=float, default=60.0)
    complete = senses_sub.add_parser("completeness", help="invariant I7 over a window")
    complete.add_argument("--since", required=True, help="ISO-8601 start of the window")
    rehearse = senses_sub.add_parser("rehearse", help="R-007 on Ledger FAIL verdicts, end to end")
    rehearse.add_argument("--tasks", type=int, default=6)
    rehearse.add_argument("--seed", type=int, default=11)

    keys = sub.add_parser("keys", help="attestation key custody (spec §4.5.1)")
    keys_sub = keys.add_subparsers(dest="keys_command", required=True)
    generate = keys_sub.add_parser(
        "generate", help="generate the Ed25519 attestation key; prints only the public half"
    )
    generate.add_argument("--private-key", type=Path, required=True, help="outside any repo")
    generate.add_argument("--public-key", type=Path, default=Path(".kintsugi/attest.pub"))
    return parser


def _inside_git_worktree(path: Path) -> bool:
    return any((parent / ".git").exists() for parent in path.resolve().parents)


def _keys_generate(private_key: Path, public_key: Path) -> int:
    if _inside_git_worktree(private_key):
        print(f"refusing: {private_key} is inside a git work tree; keep the key out of any repo")
        return EXIT_FAIL
    signer = Ed25519Signer.generate()
    try:
        signer.save(private_key, public_key)
    except FileExistsError as error:
        print(f"refusing: {error}")
        return EXIT_FAIL
    print(signer.public_key_openssh())
    print(f"fingerprint {fingerprint(load_public_key(public_key))}")
    print(f"public key written to {public_key}; private key kept at {private_key} (0600)")
    return EXIT_OK


def _repo() -> Path:
    return Path.cwd()


def _holdout(args: argparse.Namespace) -> int:
    from kintsugi.fleet.hosts import load_hosts
    from kintsugi.gates import holdout

    repo = _repo()
    if args.holdout_command == "keygen":
        try:
            recipient = holdout.keygen(args.identity, repo)
        except holdout.HoldoutError as error:
            print(f"refusing: {error}")
            return EXIT_FAIL
        print(f"recipient {recipient} written to {holdout.RECIPIENT}; identity kept outside")
        return EXIT_OK
    if args.holdout_command == "seal":
        hosts = load_hosts(repo)
        for host_id in args.host or sorted(hosts):
            spec = hosts[host_id]
            report = holdout.seal(repo, spec.id, spec.package, spec.root)
            relative = report.path.resolve().relative_to(repo.resolve()).as_posix()
            print(f"{spec.id}: {report.tasks} tasks sealed in {relative} sha256:{report.sha256} "
                  f"canary sha256:{report.canary_sha256}")  # fmt: skip
        return EXIT_OK
    problems = holdout.verify(repo)
    for problem in problems:
        print(f"FAIL {problem}")
    print("holdout manifest: OK" if not problems else f"holdout manifest: {len(problems)} problems")
    return EXIT_FAIL if problems else EXIT_OK


def _fleet_baseline(args: argparse.Namespace) -> int:
    import json

    from kintsugi.fleet.baseline import baseline
    from kintsugi.fleet.fabric import FabricConfig, load_dotenv
    from kintsugi.fleet.hosts import load_hosts
    from kintsugi.gates import holdout

    repo = _repo()
    secrets = load_dotenv(repo / ".env")
    identity = None
    if args.holdout_identity:
        identity = args.holdout_identity.read_text(encoding="ascii").strip()
    neatlogs = {k: secrets[k] for k in ("NEATLOGS_API_KEY",) if k in secrets}
    hosts = load_hosts(repo)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    failed = False
    for host_id in args.host or sorted(hosts):
        spec = hosts[host_id]
        sealed = holdout.holdout_file(spec.package, spec.root)
        holdout_jsonl = None
        if identity and sealed.is_file():
            holdout_jsonl = holdout.tasks_jsonl(holdout.open_holdout(sealed, identity))
        config = FabricConfig(seed=0, anthropic_key=secrets.get("ANTHROPIC_API_KEY"))
        card = baseline(spec, args.seeds, holdout_jsonl=holdout_jsonl, config=config,
                        workers=args.workers, extra_env=neatlogs)  # fmt: skip
        out = args.out_dir / f"{spec.id}.json"
        out.write_text(json.dumps(card, indent=1) + "\n", encoding="utf-8", newline="\n")
        pooled = card["pooled"]
        print(f"{spec.id}: {pooled['passed']}/{pooled['n']} PASS "
              f"({pooled['pass_rate']:.3f}, Wilson95 {pooled['wilson95']}) "
              f"p95 {card['p95_latency_s']}s model={card['model_mode']} "
              f"splits={card['splits']} meets_slo={card['meets_slo']} -> {out}")  # fmt: skip
        failed |= not card["meets_slo"]
    return EXIT_FAIL if failed else EXIT_OK


def _fleet_up(args: argparse.Namespace) -> int:
    from kintsugi.fleet.fabric import load_dotenv
    from kintsugi.fleet.hosts import load_hosts
    from kintsugi.fleet.runtime import STATE, Deployer, Runtime

    repo = _repo()
    secrets = load_dotenv(repo / ".env")
    hosts = load_hosts(repo)
    chosen = {h: hosts[h] for h in (args.host or sorted(hosts))}
    deployer = None if args.no_deploy else Deployer(repo, repo / STATE / "deploy", args.ref)
    neatlogs = {k: secrets[k] for k in ("NEATLOGS_API_KEY",) if k in secrets}
    key = secrets.get("ANTHROPIC_API_KEY")
    runtime = Runtime(repo, chosen, seed=args.seed, deployer=deployer, scale=args.scale,
                      anthropic_key=key, extra_env=neatlogs)  # fmt: skip
    print(f"fleet up: {sorted(chosen)} seed={args.seed} deploy={args.ref if deployer else 'off'}"
          " — stop with `kintsugi fleet down`")  # fmt: skip
    with contextlib.suppress(KeyboardInterrupt):
        runtime.run(duration_s=args.duration)
    print(f"fleet down: live tasks run {runtime.counts}")
    return EXIT_OK


def _fleet_down() -> int:
    from kintsugi.fleet.runtime import STOP_FILE

    stop = _repo() / STOP_FILE
    stop.parent.mkdir(parents=True, exist_ok=True)
    stop.write_text("stop\n", encoding="utf-8")
    print(f"fleet down: requested ({STOP_FILE})")
    return EXIT_OK


def _senses(args: argparse.Namespace) -> int:
    import threading
    from datetime import UTC, datetime

    from kintsugi.fleet.fabric import load_dotenv
    from kintsugi.senses import detections, service
    from kintsugi.senses.completeness import completeness, reported_hits
    from kintsugi.senses.store import SenseStore

    repo = _repo()
    if args.senses_command == "provision" and args.dry_run:
        rendered = detections.plan()
        for item in rendered.detections:
            count, window = item.alert.get("thresholdCount", 1), item.alert.get("windowMinutes", 0)
            print(f"neatlogs  {item.name:<22} {item.detection['type']:<10} "
                  f"alert >={count} in {window}m")  # fmt: skip
        for metric in rendered.metrics:
            print(f"omega     {metric.detection_id:<30} {metric.field} {metric.operator} "
                  f"{metric.threshold:g}")  # fmt: skip
        return EXIT_OK
    env = load_dotenv(repo / ".env")
    store = SenseStore(repo / service.STORE_PATH)
    if args.senses_command == "ingress":
        print(f"senses ingress: http://{args.host}:{args.port}/hook/<secret> (state "
              f"{service.STORE_PATH})")  # fmt: skip
        service.serve(service.ingress_app(repo, store), host=args.host, port=args.port)
        return EXIT_OK
    with service.api_from_env(env) as api:
        if args.senses_command == "provision":
            manifest, changes = service.provision(repo, api, args.public_url)
            print(f"senses provision: {len(manifest.detections)} detections, "
                  f"{len(manifest.metrics)} omega receptors, webhook "
                  f"{'registered' if manifest.webhook_id else 'not registered (no --public-url)'}"
                  f" -> {service.MANIFEST_PATH}")  # fmt: skip
            for change in changes:
                print(f"  {change}")
            return EXIT_OK
        if args.senses_command == "poll":
            poll = service.poller(repo, api, store)
            if args.once:
                for report in poll.run_once():
                    print(f"{report.detection_id}: {report.seen} seen, {report.new} new, "
                          f"{report.duplicates} already claimed, {report.pages} pages")  # fmt: skip
                return EXIT_OK
            print(f"senses poll: every {args.interval:g}s — Ctrl+C to stop")
            stop = threading.Event()
            with contextlib.suppress(KeyboardInterrupt):
                service.poll_forever(poll, stop, args.interval)
            return EXIT_OK
        if args.senses_command == "rehearse":
            from kintsugi.senses.rehearsal import rehearse

            manifest = service.load_manifest(repo)
            result = rehearse(repo, api, manifest, service.poller(repo, api, store),
                              tasks=args.tasks, seed=args.seed)  # fmt: skip
            new = sum(s.new for s in result.sweeps)
            print(f"rehearsal: {len(result.failed_tasks)}/{len(result.rows)} Ledger tasks FAIL "
                  f"under the FX outage; R-007 hits on {result.hits} of them after "
                  f"{result.waited_s:.0f}s; poller claimed {new} new hits "
                  f"({store.pending()} queued)")  # fmt: skip
            detected = bool(result.failed_tasks) and result.hits == len(result.failed_tasks)
            return EXIT_OK if detected else EXIT_FAIL
        start = datetime.fromisoformat(args.since.replace("Z", "+00:00")).astimezone(UTC)
        manifest = service.load_manifest(repo)
        hits = reported_hits(api, manifest.detections, start)
        report = completeness(hits, store.seen_hits(), start, datetime.now(UTC))
        print(f"I7 completeness since {start.isoformat()}: {report.summary()}")
        return EXIT_OK if report.ok else EXIT_FAIL


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "version":
        print(f"kintsugi {__version__}")
        for tool, version in PINNED_TOOLCHAIN.items():
            print(f"  {tool:<13} {version}")
        return EXIT_OK
    if args.command == "keys":
        return _keys_generate(args.private_key, args.public_key)
    if args.command == "holdout":
        return _holdout(args)
    if args.command == "senses":
        return _senses(args)
    if args.command == "fleet" and args.fleet_command == "baseline":
        return _fleet_baseline(args)
    if args.command == "fleet":
        return _fleet_up(args) if args.fleet_command == "up" else _fleet_down()

    stage = _PENDING[args.command]
    print(f"kintsugi {args.command}: not built yet — arrives in {stage}. No checks were run.")
    return EXIT_SKIPPED


if __name__ == "__main__":
    sys.exit(main())

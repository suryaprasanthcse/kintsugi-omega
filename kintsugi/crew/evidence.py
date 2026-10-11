"""The evidence of one incident, aggregate-only (spec §7.4 in miniature; Stage 8 bundles it).

``summarize`` reads an incident's case file, ledger and repository after it ends and writes what
a reviewer needs to check the run without trusting it: the transition log, G0, the intent
record (quotes and the two-way checkpoint mapping), every hypothesis with its gate statuses, G4
aggregates, commit and checkpoint, the decisions and dispute, the L4 records, the shipment,
costs by role, audit checks C2–C5 on every attested commit, the I5 leak scan (holdout canary
hits in the case file, the crew's prompts and replies, and the Surgeon transcripts) and which
artifact kinds each role read. No holdout task, no prompt text, no secret is copied out.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from kintsugi import PINNED_TOOLCHAIN
from kintsugi.control import contracts as c
from kintsugi.crew import claude_session
from kintsugi.gates import holdout
from kintsugi.proof import attest
from kintsugi.proof.commit import verify_commit
from kintsugi.proof.ledger import Ledger


def _leak_scan(ctx: Any, incident: str, canary: str) -> dict[str, int]:
    case_root = ctx.state / "cases" / incident
    case_bytes = b"".join(p.read_bytes() for p in sorted(case_root.rglob("*")) if p.is_file())
    view = ctx.case(incident)
    transcripts = 0
    sessions = 0
    for name in view.names("surgeon_session"):
        record = view._load("surgeon_session", name)
        path = claude_session.find_transcript(str(record.get("session")))
        if path is not None:
            sessions += 1
            transcripts += holdout.canary_hits(path.read_bytes(), canary)
    return {
        "case_file": holdout.canary_hits(case_bytes, canary),
        "crew_calls": sum(holdout.canary_hits(str(call).encode(), canary) for call in view.calls()),
        "surgeon_transcripts": transcripts,
        "transcripts_scanned": sessions,
    }


def summarize(
    cfg: Any,
    ctx: Any,
    crew: Any,
    signer: attest.Ed25519Signer,
    delivery: Any,
    incident: str,
    outcome: c.Outcome,
    store: Any,
    github: dict[str, Any],
    *,
    duration_s: float,
) -> dict[str, Any]:
    case = ctx.case(incident)
    raw = case._load  # the evidence tool reads every artifact; it copies aggregates only
    target = ctx.targets["ledger-practice"]
    names = {
        kind: case.names(kind)
        for kind in ("card", "verdict", "outcome", "decision", "experiment", "reverify", "refusal")
    }
    public = attest.load_public_key(signer.public_key_openssh())
    hypotheses = []
    for hyp in names["card"]:
        card = raw("card", hyp)
        verdict = raw("verdict", hyp) if hyp in names["verdict"] else {}
        out = raw("outcome", hyp) if hyp in names["outcome"] else {}
        checks = None
        if out.get("commit"):
            report = verify_commit(target.repo, out["commit"], public_key=public)
            checks = {k: (not v) for k, v in report.problems.items()}
        hypotheses.append(
            {
                "hypothesis": hyp,
                "round": card.get("round"),
                "title": card.get("title"),
                "allowed_files": card.get("allowed_files"),
                "commit": out.get("commit"),
                "checkpoint": out.get("checkpoint"),
                "lines": out.get("lines"),
                "refused": raw("refusal", hyp).get("reason") if hyp in names["refusal"] else None,
                "gates": {g: v.get("status") for g, v in (verdict.get("gates") or {}).items()},
                "gate_summaries": {
                    g: v.get("summary")
                    for g, v in (verdict.get("gates") or {}).items()
                    if v.get("status") != "pass"
                },
                "g4": verdict.get("g4") or {},
                "passed": verdict.get("passed"),
                "c2_c5": checks,
            }
        )
    record = raw("intent_record", "record") if case.exists("intent_record", "record") else {}
    report = raw("cause", "report") if case.exists("cause", "report") else {}
    repro = raw("repro", "g0") if case.exists("repro", "g0") else {}
    signature = raw("signature", "phi") if case.exists("signature", "phi") else {}
    payload = holdout.open_holdout(
        target.repo / str(target.holdout), cfg.identity.read_text(encoding="ascii").strip()
    )
    leaks = _leak_scan(ctx, incident, str(payload["canary"]))
    del payload
    accesses = Counter((a["role"], a["op"], a["kind"]) for a in case.accesses())
    dispositions = Counter(h.disposition for h in store.seen_hits().values())
    entries = crew.ledger.entries()
    ledger_ok = Ledger(crew.ledger.genesis, entries).head == (
        entries[-1]["hash"] if entries else crew.ledger.genesis
    )
    delivery_check = verify_commit(target.repo, delivery.commit, public_key=public)
    return {
        "run": cfg.run,
        "kind": "stage-6 ledger rehearsal (unsealed, disclosed practice target)",
        "toolchain": PINNED_TOOLCHAIN,
        "duration_s": round(duration_s, 1),
        "models": dict(
            Counter(
                f"{call['role']}:{call['provider']}:{call.get('model')}" for call in case.calls()
            )
        ),
        "attestation_key": signer.public_key_openssh(),
        "delivery": {
            "commit": delivery.commit,
            "checkpoint": delivery.checkpoint,
            "prompt_sha": delivery.prompt_sha,
            "c2_c5": {k: not v for k, v in delivery_check.problems.items()},
        },
        "detection": {
            "hits": sum(dispositions.values()),
            "dispositions": {str(k): v for k, v in dispositions.items()},
            "incident": incident,
        },
        "signature": {k: signature.get(k) for k in ("phi", "label", "size", "clusters")},
        "g0": {k: repro.get(k) for k in ("ok", "seed", "split", "cassette_sha", "base_sha")}
        | {
            "failing_tasks": len(repro.get("failing") or []),
            "replays": [
                a.get("evidence", {}).get("replays_failing") for a in repro.get("attempts") or []
            ],
        },
        "intent_record": {
            k: record.get(k)
            for k in (
                "stated_goal",
                "behavioral_contract",
                "checkpoint",
                "commit",
                "confidence",
                "transcript_sha256",
                "dropped_clauses",
            )
        },
        "checkpoint_mapping": report.get("checkpoint_mapping"),
        "regression": {
            k: report.get(k)
            for k in ("regression", "first_bad_checkpoint", "bisect", "originating_commit")
        },
        "plan": raw("plan", "plan") if case.exists("plan", "plan") else {},
        "hypotheses": hypotheses,
        "decisions": {n: raw("decision", n) for n in names["decision"]},
        "experiments": {n: raw("experiment", n) for n in names["experiment"]},
        "reverify": {n: raw("reverify", n) for n in names["reverify"]},
        "outcome": {
            "state": outcome.state,
            "reason": outcome.reason,
            "transitions": [f"{t.source}->{t.target} ({t.outcome})" for t in outcome.transitions],
            "shipment": vars(outcome.shipment) if outcome.shipment else None,
        },
        "github": github,
        "costs": {"by_role": case.costs_by_role(), "total_usd": case.spent()},
        "i5_leak_scan": leaks,
        "information_flow": {f"{r}:{op}:{k}": n for (r, op, k), n in sorted(accesses.items())},
        "ledger": {
            "entries": len(entries),
            "verifies": ledger_ok,
            "types": dict(Counter(e["type"] for e in entries)),
        },
        "attempt_refs": [line for line in _refs(target.repo, incident) if line],
    }


def _refs(repo: Path, incident: str) -> list[str]:
    from kintsugi.sandbox import proc

    out = proc.run(
        [
            "git",
            "-C",
            str(repo),
            "for-each-ref",
            "--format=%(refname) %(objectname)",
            f"refs/kintsugi/attempts/{incident}",
        ],
        timeout_s=60,
    ).stdout
    return out.splitlines()

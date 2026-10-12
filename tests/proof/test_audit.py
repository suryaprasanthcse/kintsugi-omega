"""The audit mutation suite (plan Stage 8): every check C1–C14 passes on a clean fixture and fails
on a tampered one. Entire's hook is simulated by a real git hook, as in ``test_commit``.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from kintsugi.gates.protected_paths import load_policy
from kintsugi.memory.library import AntibodyLibrary
from kintsugi.proof import attest, audit, sealed
from kintsugi.proof import commit as c
from kintsugi.proof.anchor import Anchor, post_text, sign_anchor, tag_name, verify_anchor
from kintsugi.proof.audit import Status
from kintsugi.proof.ledger import Ledger, genesis_hash
from kintsugi.proof.passphrase import new_honeytoken, new_nonce
from kintsugi.proof.redaction import honeytoken_hits
from kintsugi.sandbox import proc
from kintsugi.senses.completeness import ReportedHit
from kintsugi.senses.store import SeenHit
from tests.helpers.checkpoints import git, user_line, write_checkpoint

REPO = Path(__file__).resolve().parents[2]
CKPT = "01M4MT4M6K467TEZ3R6J340DAB"
POLICY = load_policy()
HOOK = """#!/bin/sh
printf '\\nEntire-Checkpoint: %s\\n' "{ckpt}" >> "$1"
"""
T0 = datetime(2026, 10, 12, 13, 20, tzinfo=UTC)
T1 = T0 + timedelta(hours=1)
Signer = attest.Ed25519Signer


def _iso(at: datetime) -> str:
    return at.isoformat().replace("+00:00", "Z")


@pytest.fixture
def signer() -> Signer:
    return Signer.generate()


def _pub(signer: Signer) -> Any:
    return attest.load_public_key(signer.public_key_openssh())


def _ledger(signer: Signer, n: int = 4) -> tuple[str, list[dict[str, Any]]]:
    genesis = genesis_hash(_iso(T0), signer.public_key_raw())
    book = Ledger(genesis)
    for i in range(n):
        at = _iso(T0 + timedelta(minutes=5 * (i + 1)))
        book.append({"type": "incident", "incident": f"INC-{i}", "at": at, "state": "RESOLVED"})
    return genesis, list(book.entries)


def _anchor(signer: Signer, entries: list[dict[str, Any]], seq: int) -> Anchor:
    return sign_anchor(tag_name(T0 + timedelta(minutes=30)), seq, entries[seq - 1]["hash"], signer)


# ---- anchors and C1 -----------------------------------------------------------------------


def test_anchor_round_trip_and_post_text(signer: Signer) -> None:
    _genesis, entries = _ledger(signer)
    anchor = _anchor(signer, entries, 3)
    assert anchor.tag == "anchor-20261012-13"
    assert verify_anchor(anchor, _pub(signer))
    assert Anchor.from_message(anchor.tag, anchor.message()) == anchor
    assert anchor.h in post_text(anchor, "https://github.com/o/r")
    with pytest.raises(ValueError, match="carries the anchor of"):
        Anchor.from_message("anchor-20261012-14", anchor.message())


def test_c1_passes_on_a_clean_chain(signer: Signer) -> None:
    genesis, entries = _ledger(signer)
    result = audit.c1_ledger(entries, genesis, [_anchor(signer, entries, 3)], _pub(signer))
    assert result.status is Status.PASS, result.problems
    assert result.summary == "4 entries, 1 anchors"


def test_c1_catches_a_reordered_ledger(signer: Signer) -> None:
    genesis, entries = _ledger(signer)
    swapped = [entries[0], entries[2], entries[1], entries[3]]
    result = audit.c1_ledger(swapped, genesis, [_anchor(signer, entries, 3)], _pub(signer))
    assert result.status is Status.FAIL
    assert "seq 2" in result.problems[0]


def test_c1_catches_a_rewritten_chain_through_its_anchor(signer: Signer) -> None:
    genesis, entries = _ledger(signer)
    anchor = _anchor(signer, entries, 3)
    rewritten = Ledger(genesis)
    for entry in entries:
        body = {k: v for k, v in entry.items() if k not in ("seq", "prev", "hash")}
        rewritten.append({**body, "state": "ESCALATED"} if entry["seq"] == 2 else body)
    result = audit.c1_ledger(list(rewritten.entries), genesis, [anchor], _pub(signer))
    assert result.status is Status.FAIL
    assert any("anchor h_3" in p for p in result.problems)


def test_c1_catches_a_forged_anchor_and_a_missing_one(signer: Signer) -> None:
    genesis, entries = _ledger(signer)
    forged = _anchor(Signer.generate(), entries, 3)
    result = audit.c1_ledger(entries, genesis, [forged], _pub(signer))
    assert any("signature" in p for p in result.problems)
    assert (
        "no anchor covers the ledger"
        in audit.c1_ledger(entries, genesis, [], _pub(signer)).problems
    )


def test_c1_catches_a_bundle_the_ledger_did_not_record(signer: Signer) -> None:
    genesis = genesis_hash(_iso(T0), signer.public_key_raw())
    book = Ledger(genesis)
    book.append({"type": "incident", "incident": "INC-1", "at": _iso(T0), "bundle": "sha256:aa"})
    entries = list(book.entries)
    anchors = [_anchor(signer, entries, 1)]
    ok = audit.c1_ledger(entries, genesis, anchors, _pub(signer), {"INC-1": "sha256:aa"})
    assert ok.status is Status.PASS
    bad = audit.c1_ledger(entries, genesis, anchors, _pub(signer), {"INC-1": "sha256:bb"})
    assert bad.status is Status.FAIL


# ---- C2–C5 on real commits ----------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    if proc.which("git") is None:
        pytest.skip("git is not installed")
    path = tmp_path / "host"
    proc.run(["git", "init", "-q", "-b", "main", str(path)], timeout_s=30, check=True)
    git(path, "config", "user.name", "Surya Prasanth R")
    git(path, "config", "user.email", "suryaprasanthcse@gmail.com")
    (path / "reconcile.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "base")
    hook = path / ".git" / "hooks" / "prepare-commit-msg"
    hook.write_text(HOOK.format(ckpt=CKPT), encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    return path


def _attested_commit(repo: Path, signer: Signer, *, prompt: str | None = None) -> str:
    nonce = new_nonce()
    text = f"Fix the duplicate journal lines.\nKintsugi-Nonce: {nonce}\n"
    (repo / "reconcile.py").write_text("def run():\n    return 2\n", encoding="utf-8")
    transcript = user_line(prompt if prompt is not None else text)
    write_checkpoint(repo, CKPT, transcript + b'{"type":"assistant"}\n',
                     files_touched=("reconcile.py",))  # fmt: skip
    att = attest.SessionAttestation(
        nonce=nonce, role="surgeon", hypothesis="H1", incident="INC-0042",
        worktree="wt/INC-0042/H1", base_sha="0" * 40, prompt_template_sha=c.prompt_digest(text),
        t_start=_iso(T0),
    )  # fmt: skip
    outcome = c.orchestrator_commit(repo, attestation=att, signer=signer, subject="fix: hoist",
                                    policy=POLICY)  # fmt: skip
    return outcome.sha


def test_c2_to_c5_pass_on_an_attested_commit(repo: Path, signer: Signer) -> None:
    sha = _attested_commit(repo, signer)
    result = audit.c2_c5(repo, [sha], _pub(signer))
    assert all(r.status is Status.PASS for r in result.results.values()), result.results
    assert result.human_commits == 0


def test_c2_catches_a_human_commit(repo: Path, signer: Signer) -> None:
    (repo / "reconcile.py").write_text("def run():\n    return 3\n", encoding="utf-8")
    git(repo, "commit", "-q", "-am", "quick fix")
    sha = git(repo, "rev-parse", "HEAD")
    result = audit.c2_c5(repo, [sha], _pub(signer))
    assert result.results["C2"].status is Status.FAIL
    assert result.human_commits == 1
    assert all(result.results[k].status is Status.FAIL for k in ("C3", "C4", "C5"))


def test_c3_catches_a_forged_signature(repo: Path, signer: Signer) -> None:
    sha = _attested_commit(repo, signer)
    result = audit.c2_c5(repo, [sha], _pub(Signer.generate()))
    assert result.results["C3"].status is Status.FAIL
    assert result.results["C2"].status is Status.PASS


def test_c4_catches_a_missing_nonce(repo: Path, signer: Signer) -> None:
    sha = _attested_commit(repo, signer, prompt="Fix the duplicate journal lines.\n")
    result = audit.c2_c5(repo, [sha], _pub(signer))
    assert result.results["C4"].status is Status.FAIL
    assert "nonce" in result.results["C4"].problems[0]


def test_c5_catches_a_prompt_mismatch(repo: Path, signer: Signer) -> None:
    sha = _attested_commit(repo, signer)
    nonce = attest.parse_trailers(git(repo, "log", "-1", "--format=%B"))["Kintsugi-Nonce"][0]
    other = f"Fix the duplicate journal lines!\nKintsugi-Nonce: {nonce}\n"
    write_checkpoint(repo, CKPT, user_line(other), files_touched=("reconcile.py",))
    result = audit.c2_c5(repo, [sha], _pub(signer))
    assert result.results["C5"].status is Status.FAIL
    assert result.results["C4"].status is Status.PASS


def test_a_clean_merge_is_accepted_and_a_doctored_one_is_not(repo: Path, signer: Signer) -> None:
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "-b", "fix")
    fix = _attested_commit(repo, signer)
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "-m", "Merge pull request #1", fix)
    merge = git(repo, "rev-parse", "HEAD")
    assert audit.merge_problem(repo, merge, [base, fix]) is None
    (repo / "extra.py").write_text("x = 1\n", encoding="utf-8")
    git(repo, "add", "extra.py")
    git(repo, "commit", "-q", "--amend", "--no-edit")
    doctored = git(repo, "rev-parse", "HEAD")
    assert audit.merge_problem(repo, doctored, [base, fix]) is not None
    result = audit.c2_c5(repo, [fix, doctored], _pub(signer))
    assert result.results["C2"].status is Status.FAIL


# ---- C6 -----------------------------------------------------------------------------------


def _spec(sid: str = "S-90") -> dict[str, Any]:
    return {"id": sid, "class": "tool.retry.state_reset", "target": {"host": "practice"},
            "delivery": {"kind": "pr"}, "ground_truth": {"files": ["a.py"]},
            "schedule": {"deliver_at": _iso(T0 + timedelta(minutes=10))},
            "honeytoken": new_honeytoken(), "seed": 7}  # fmt: skip


def _sealed(spec: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    seal = sealed.seal(spec)
    record = sealed.commitment_record(spec, seal, sealed_at=_iso(T0 - timedelta(minutes=5)),
                                      not_before=_iso(T0 + timedelta(minutes=5)))  # fmt: skip
    book = Ledger("0" * 64)
    book.append({"type": "sabotage", "event": "sealed", "sabotage": spec["id"], "c": record["c"],
                 "at": _iso(T0)})  # fmt: skip
    book.append({"type": "sabotage", "event": "delivered", "sabotage": spec["id"],
                 "at": _iso(T0 + timedelta(minutes=10))})  # fmt: skip
    return record, sealed.reveal_record(spec, seal), list(book.entries)


def test_c6_passes_when_the_reveal_opens_the_commitment() -> None:
    record, reveal, entries = _sealed(_spec())
    result = audit.c6_sabotage([record], {"S-90": reveal}, entries, lambda t: [],
                               window_closed=True)  # fmt: skip
    assert result.status is Status.PASS, result.problems
    assert result.summary == "1/1 commitments, 0 honeytoken hits"


def test_c6_catches_a_changed_spec_and_a_wrong_family() -> None:
    record, reveal, entries = _sealed(_spec())
    changed = {**reveal, "spec": {**reveal["spec"], "seed": 8}}
    result = audit.c6_sabotage([record], {"S-90": changed}, entries, lambda t: [],
                               window_closed=True)  # fmt: skip
    assert "does not open" in result.problems[0]
    wrong = {**record, "class_family": "tool.cache"}
    result = audit.c6_sabotage([wrong], {"S-90": reveal}, entries, lambda t: [],
                               window_closed=True)  # fmt: skip
    assert any("class family" in p for p in result.problems)


def test_c6_catches_a_delivery_before_the_seal() -> None:
    record, reveal, entries = _sealed(_spec())
    body = [{k: v for k, v in e.items() if k not in ("seq", "prev", "hash")} for e in entries]
    book = Ledger("0" * 64)
    for entry in reversed(body):
        book.append(entry)
    result = audit.c6_sabotage([record], {"S-90": reveal}, list(book.entries), lambda t: [],
                               window_closed=True)  # fmt: skip
    assert any("before it was sealed" in p for p in result.problems)


def test_c6_skips_while_sealed_and_fails_if_never_revealed() -> None:
    record, _reveal, entries = _sealed(_spec())
    open_ = audit.c6_sabotage([record], {}, entries, lambda t: [], window_closed=False)
    assert open_.status is Status.SKIP
    closed = audit.c6_sabotage([record], {}, entries, lambda t: [], window_closed=True)
    assert closed.status is Status.FAIL


def test_c6_honeytoken_positive_control(repo: Path) -> None:
    """A planted honeytoken in a checkpoint transcript must be found (spec risk N1)."""
    spec = _spec()
    record, reveal, entries = _sealed(spec)
    write_checkpoint(repo, CKPT, user_line(f"context {spec['honeytoken']} leaked"))
    hits = lambda token: honeytoken_hits(repo, token)  # noqa: E731
    result = audit.c6_sabotage([record], {"S-90": reveal}, entries, hits, window_closed=True)
    assert result.status is Status.FAIL
    assert "honeytoken found" in result.problems[0]
    clean = _spec("S-90")
    record2, reveal2, entries2 = _sealed(clean)
    assert audit.c6_sabotage([record2], {"S-90": reveal2}, entries2, hits,
                             window_closed=True).status is Status.PASS  # fmt: skip


# ---- online checks with recorded API answers ------------------------------------------------


def test_c7_needs_a_passing_g7_run_per_pr() -> None:
    prs = {"https://github.com/o/r/pull/7": "INC-1", "https://github.com/o/r/pull/8": "INC-2"}
    runs = {"7": [{"name": "omega-ci / G7 holdout", "bucket": "pass"}],
            "8": [{"name": "omega-ci / lint", "bucket": "pass"}]}  # fmt: skip
    result = audit.c7_ci(prs, lambda url: runs[url.rsplit("/", 1)[1]], "G7")
    assert result.status is Status.FAIL
    assert result.problems == ("INC-2 https://github.com/o/r/pull/8: no G7 run",)
    runs["8"] = [{"name": "G7", "bucket": "pass"}]
    assert audit.c7_ci(prs, lambda url: runs[url.rsplit("/", 1)[1]], "G7").status is Status.PASS
    runs["8"] = [{"name": "G7", "bucket": "fail"}]
    assert audit.c7_ci(prs, lambda url: runs[url.rsplit("/", 1)[1]], "G7").status is Status.FAIL
    assert audit.c7_ci(prs, None, "G7").status is Status.SKIP


def test_c8_catches_an_unknown_trace_and_one_outside_the_window() -> None:
    wanted = {"tr-1": ("INC-1", T0, T1), "tr-2": ("INC-1", T0, T1)}
    known = {"tr-1": T0 + timedelta(minutes=3), "tr-2": T0 + timedelta(minutes=4)}
    assert audit.c8_traces(wanted, known.get).status is Status.PASS
    del known["tr-2"]
    assert "does not exist" in audit.c8_traces(wanted, known.get).problems[0]
    known["tr-2"] = T0 - timedelta(days=1)
    assert "outside" in audit.c8_traces(wanted, known.get).problems[0]
    assert audit.c8_traces(wanted, None).status is Status.SKIP


def _hit(n: int, *, incident: str | None, disposition: str | None) -> tuple[ReportedHit, SeenHit]:
    reported = ReportedHit("det-1", f"tr-{n}", "sp-1", T0 + timedelta(minutes=n))
    seen = SeenHit(reported.key, None, "det-1", f"tr-{n}", "sp-1", "webhook", "R-001", "ledger",
                   _iso(reported.detected_at), incident, disposition)  # fmt: skip
    return reported, seen


def test_c9_catches_an_unmatched_hit_and_an_incident_missing_from_the_ledger() -> None:
    pairs = [_hit(1, incident="INC-1", disposition="opened"),
             _hit(2, incident="INC-1", disposition="attached")]  # fmt: skip
    reported = [r for r, _ in pairs]
    seen = {s.hit_key: s for _, s in pairs}
    ok = audit.c9_completeness(reported, seen, T0, T1, {"INC-1"})
    assert ok.status is Status.PASS
    assert ok.summary == "2 hits → 1 incidents + 1 dedupes"
    extra, _ = _hit(3, incident=None, disposition=None)
    assert audit.c9_completeness([*reported, extra], seen, T0, T1, {"INC-1"}).problems == (
        f"hit {extra.key}: never claimed",
    )
    assert audit.c9_completeness(reported, seen, T0, T1, set()).status is Status.FAIL
    assert audit.c9_completeness(None, None, T0, T1, set()).status is Status.SKIP


def test_c10_tolerates_one_percent_and_no_more() -> None:
    assert audit.c10_cost(1.005, {"s1": 0.5, "s2": 0.5}).status is Status.PASS
    result = audit.c10_cost(1.02, {"s1": 0.5, "s2": 0.5})
    assert result.status is Status.FAIL
    assert "Δ 2.0%" in result.problems[0]
    assert audit.c10_cost(1.0, {"s1": 1.0, "s2": None}).status is Status.FAIL
    assert audit.c10_cost(1.0, None).status is Status.SKIP


def _version(at: datetime, enforcement: str = "active", ref: str = "~DEFAULT_BRANCH") -> dict:
    return {"updated_at": _iso(at), "enforcement": enforcement, "include": [ref]}


def test_c12_catches_a_ruleset_gap() -> None:
    created = _version(T0 - timedelta(minutes=30))
    assert audit.c12_rulesets({1: [created]}, T0, T1, "main").status is Status.PASS
    disabled = _version(T0 + timedelta(minutes=20), "disabled")
    gap = audit.c12_rulesets({1: [created, disabled, _version(T0 + timedelta(minutes=25))]},
                             T0, T1, "main")  # fmt: skip
    assert gap.status is Status.FAIL
    assert "not active on main" in gap.problems[0]
    late = audit.c12_rulesets({1: [_version(T0 + timedelta(minutes=1))]}, T0, T1, "main")
    assert any("did not exist at T0" in p for p in late.problems)
    other = audit.c12_rulesets({1: [_version(T0 - timedelta(hours=1), ref="refs/heads/dev")]},
                               T0, T1, "main")  # fmt: skip
    assert other.status is Status.FAIL
    assert audit.c12_rulesets({}, T0, T1, "main").status is Status.FAIL
    assert audit.c12_rulesets(None, T0, T1, "main").status is Status.SKIP


# ---- C11, C13, C14 ------------------------------------------------------------------------


@pytest.fixture
def library(tmp_path: Path) -> AntibodyLibrary:
    root = tmp_path / "antibodies"
    shutil.copytree(REPO / "antibodies", root)
    return AntibodyLibrary(root)


def _edit_lineage(library: AntibodyLibrary, change: Any) -> None:
    path = library.root / "AB-1" / "lineage.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_c11_passes_on_the_real_ab1_lineage(library: AntibodyLibrary) -> None:
    result = audit.c11_lineage(library)
    assert result.status is Status.PASS, result.problems
    assert result.summary == "1 antibodies, 1 quarantine"


@pytest.mark.parametrize(
    ("change", "expect"),
    [
        (
            lambda d: d["events"].pop(
                next(i for i, e in enumerate(d["events"]) if e["event"] == "refined")
            ),
            "refinement",
        ),
        (
            lambda d: d["events"].pop(
                next(i for i, e in enumerate(d["events"]) if e.get("to") == "QUARANTINED")
            ),
            "state",
        ),
        (
            lambda d: d["events"].pop(
                next(i for i, e in enumerate(d["events"]) if e["event"] == "unprotected")
            ),
            "hosts",
        ),
        (lambda d: d.update(quarantines=0), "quarantines"),
        (lambda d: d["events"][0].update(incident="INC-OTHER"), "evidence"),
    ],
    ids=["refinement-gap", "state-gap", "host-gap", "quarantine-count", "evidence-link"],
)
def test_c11_catches_a_lineage_gap(library: AntibodyLibrary, change: Any, expect: str) -> None:
    _edit_lineage(library, change)
    result = audit.c11_lineage(library)
    assert result.status is Status.FAIL
    assert any(expect in p for p in result.problems), result.problems


def test_c11_catches_a_missing_version_file(library: AntibodyLibrary) -> None:
    (library.root / "AB-1" / "versions" / "1.0.yaml").unlink()
    assert any("version files" in p for p in audit.c11_lineage(library).problems)


def _holdout_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "h"
    sealed_file = repo / "pkg" / "evals" / "holdout" / "tasks.jsonl.age"
    sealed_file.parent.mkdir(parents=True)
    sealed_file.write_bytes(b"age-encrypted bytes")
    import hashlib

    digest = hashlib.sha256(sealed_file.read_bytes()).hexdigest()
    manifest = repo / ".kintsugi" / "holdout.sha256"
    manifest.parent.mkdir()
    manifest.write_text(f"# header\n{digest}  pkg/evals/holdout/tasks.jsonl.age\n",
                        encoding="utf-8")  # fmt: skip
    return repo


def test_c13_catches_a_changed_holdout_and_a_changed_manifest(tmp_path: Path) -> None:
    repo = _holdout_repo(tmp_path)
    at_t0 = audit.manifest_digest(repo)
    assert audit.c13_holdout(repo, at_t0).status is Status.PASS
    assert audit.c13_holdout(repo, None).status is Status.SKIP
    (repo / "pkg" / "evals" / "holdout" / "tasks.jsonl.age").write_bytes(b"swapped")
    assert "hash changed" in audit.c13_holdout(repo, at_t0).problems[0]
    repo2 = _holdout_repo(tmp_path / "second")
    manifest = repo2 / ".kintsugi" / "holdout.sha256"
    manifest.write_text(manifest.read_text(encoding="utf-8") + "# re-sealed\n", encoding="utf-8")
    assert audit.c13_holdout(repo2, at_t0).problems == ("the holdout manifest changed since T0",)


def test_c14_catches_a_self_repair_touching_the_proof_plane() -> None:
    clean = audit.c14_self_repair({"a" * 40: ["kintsugi/crew/scout.py"]}, POLICY)
    assert clean.status is Status.PASS
    assert clean.summary == "1/1"
    bad = audit.c14_self_repair({"b" * 40: ["kintsugi/proof/audit.py", "README.md"]}, POLICY)
    assert bad.status is Status.FAIL
    assert "kintsugi/proof/audit.py" in bad.problems[0]


# ---- the whole audit over a repository ----------------------------------------------------


def _orphan_ledger(repo: Path, text: str) -> None:
    path = repo / ".git" / "ledger.tmp"
    path.write_text(text, encoding="utf-8", newline="\n")
    blob = git(repo, "hash-object", "-w", str(path))
    path.unlink()
    tree = git(repo, "mktree", input_text=f"100644 blob {blob}\tledger.jsonl\n")
    sha = git(repo, "commit-tree", tree, "-m", "ledger")
    git(repo, "update-ref", "refs/heads/ledger", sha)


def test_run_audit_over_a_window(repo: Path, signer: Signer, tmp_path: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    now = datetime.now(UTC).replace(microsecond=0)
    t0 = _iso(now - timedelta(minutes=30))
    sha = _attested_commit(repo, signer)
    genesis = genesis_hash(t0, signer.public_key_raw())
    book = Ledger(genesis)
    book.append({"type": "incident", "incident": "INC-0042", "at": _iso(now), "state": "RESOLVED"})
    _orphan_ledger(repo, book.to_jsonl())
    anchor = sign_anchor(tag_name(now), 1, book.head, signer)
    git(repo, "tag", "-a", anchor.tag, "-m", anchor.message(), "ledger")
    raw = _pub(signer).public_bytes(Encoding.Raw, PublicFormat.Raw)
    assert genesis == genesis_hash(t0, raw)
    window = {
        "t0": t0,
        "repo": "o/r",
        "branch": "main",
        "base": base,
        "attest_pubkey": signer.public_key_openssh(),
        "holdout_manifest": "sha256:00",
    }
    (repo / ".kintsugi").mkdir()
    (repo / audit.WINDOW_FILE).write_text(json.dumps(window), encoding="utf-8")
    report = audit.run_audit(repo, library_root=REPO / "antibodies")
    status = {r.check: r.status for r in report.results}
    assert {k for k, v in status.items() if v is Status.PASS} >= {
        "C1",
        "C2",
        "C3",
        "C4",
        "C5",
        "C11",
        "C14",
    }, report.render()
    assert status["C7"] is status["C8"] is status["C12"] is Status.SKIP
    assert status["C13"] is Status.FAIL  # the fixture repository lists no sealed holdout
    assert report.human_commits == 0
    assert report.exit_code == audit.EXIT_FAIL
    later = now + timedelta(minutes=1)
    assert sha in audit.window_commits(repo, audit.Window.from_json(window), later)
    git(repo, "tag", "-d", anchor.tag)
    forged = sign_anchor(anchor.tag, 1, "f" * 64, signer)
    git(repo, "tag", "-a", forged.tag, "-m", forged.message(), "ledger")
    report = audit.run_audit(repo, selection=audit.Selection(("C1",)),
                             library_root=REPO / "antibodies")  # fmt: skip
    assert report.results[0].status is Status.FAIL


def test_exit_codes() -> None:
    passing = audit.verdict("C1", "ok")
    skipped = audit.skip("C7", "offline")
    failing = audit.verdict("C2", "1/2", ["x"])
    assert audit.AuditReport([passing]).exit_code == audit.EXIT_OK
    assert audit.AuditReport([passing, skipped]).exit_code == audit.EXIT_SKIPPED
    assert audit.AuditReport([passing, skipped, failing]).exit_code == audit.EXIT_FAIL

from __future__ import annotations

from pathlib import Path

import pytest

from kintsugi.fleet import baseline as bl
from kintsugi.fleet.fabric import Fabric, FabricConfig, load_dotenv
from kintsugi.fleet.hosts import load_hosts, resolve
from kintsugi.gates import holdout
from kintsugi.sandbox.runner import check_env

REPO = Path(__file__).resolve().parents[2]
HOSTS = load_hosts(REPO)


def test_the_registry_loads_every_host_with_its_tool_registry() -> None:
    ledger = HOSTS["ledger"]
    assert ledger.package == "ledger"
    assert ledger.side_effecting_tools() == {"post_journal_entry"}
    assert ledger.slo.pass_rate == 0.90
    assert callable(resolve(ledger.offline_model))
    with pytest.raises(ValueError, match="module:attribute"):
        resolve("ledger.agent")


def test_a_host_gets_proxies_and_placeholders_never_secrets() -> None:
    with Fabric({"ledger": HOSTS["ledger"]}, FabricConfig(seed=1)) as fabric:
        env = fabric.env("ledger")
        assert env["ANTHROPIC_BASE_URL"].endswith("/h/ledger")
        assert env["KINTSUGI_EXT_URL"].startswith("http://127.0.0.1:")
        check_env(env)  # would raise on a real credential
        assert fabric.model_mode == "offline"


def test_dotenv_parsing(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text('﻿# comment\nA=1\nB = "two"\nnot a pair\n\nC=x=y\n', encoding="utf-8")
    assert load_dotenv(path) == {"A": "1", "B": "two", "C": "x=y"}
    assert load_dotenv(tmp_path / "missing") == {}


def test_a_two_seed_baseline_meets_the_slo_and_reports_its_chaos() -> None:
    card = bl.baseline(HOSTS["ledger"], [1, 2], holdout_jsonl=None, limit=20, workers=4)
    assert card["model_mode"] == "offline"
    assert card["seeds"] == [1, 2]
    assert set(card["per_seed"]) == {"1", "2"}
    assert card["pooled"]["n"] == 40
    assert card["meets_slo"]
    assert "FAIL:DUPLICATE_ENTRY" not in card["verdicts"]
    assert card["chaos"]["fx:calls"] > 0
    assert card["cost"]["billable_usd"] == 0
    assert card["cost"]["metered_usd"] > 0


@pytest.fixture
def sealed(tmp_path: Path) -> tuple[Path, str, holdout.SealReport]:
    repo = tmp_path / "repo"
    (repo / ".kintsugi").mkdir(parents=True)
    (repo / ".git").mkdir()
    identity_path = tmp_path / "custody" / "holdout.agekey"
    holdout.keygen(identity_path, repo)
    report = holdout.seal(repo, "ledger", "ledger", repo / "hosts" / "ledger")
    return repo, identity_path.read_text().strip(), report


def test_a_sealed_holdout_opens_only_with_its_identity(
    sealed: tuple[Path, str, holdout.SealReport],
) -> None:
    _, identity, report = sealed
    assert report.tasks == 40
    payload = holdout.open_holdout(report.path, identity)
    assert len(payload["tasks"]) == 40
    canary = payload["canary"]
    assert all(canary in task["document"] for task in payload["tasks"])  # I5 leak marker
    assert holdout.canary_hits(holdout.tasks_jsonl(payload).encode(), canary) == 40
    assert canary.encode() not in report.path.read_bytes()  # unreadable without the key
    other, _ = holdout.new_identity()
    with pytest.raises(holdout.HoldoutError, match="cannot decrypt"):
        holdout.open_holdout(report.path, other)


def test_the_manifest_detects_a_changed_holdout(
    sealed: tuple[Path, str, holdout.SealReport],
) -> None:
    repo, _, report = sealed
    assert holdout.verify(repo) == []
    manifest = (repo / holdout.MANIFEST).read_text()
    assert f"# canary ledger sha256:{report.canary_sha256}" in manifest
    report.path.write_bytes(report.path.read_bytes() + b"x")
    assert holdout.verify(repo) == [f"{report.path.relative_to(repo).as_posix()}: hash changed"]


def test_holdout_keys_stay_out_of_repositories(tmp_path: Path) -> None:
    with pytest.raises(holdout.HoldoutError, match="git work tree"):
        holdout.keygen(REPO / "holdout.agekey.must-not-exist", tmp_path)
    assert not (REPO / "holdout.agekey.must-not-exist").exists()


def test_reseal_replaces_the_manifest_entry(sealed: tuple[Path, str, holdout.SealReport]) -> None:
    repo, _, first = sealed
    second = holdout.seal(repo, "ledger", "ledger", repo / "hosts" / "ledger")
    lines = (repo / holdout.MANIFEST).read_text().splitlines()
    assert sum("tasks.jsonl.age" in line for line in lines) == 1
    assert second.sha256 != first.sha256
    assert holdout.verify(repo) == []

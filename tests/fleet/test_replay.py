"""Deterministic cassette replay: the same verdicts on 3/3 replays, with no upstream calls."""

from __future__ import annotations

from pathlib import Path

import pytest

from kintsugi.fleet import replay as rp
from kintsugi.fleet.hosts import load_hosts
from kintsugi.sandbox.cassette import Cassette

HOSTS = load_hosts(Path(__file__).resolve().parents[2])


@pytest.mark.parametrize(("host", "limit"), [("ledger", 20), ("scout", 12), ("triage", 12)])
def test_replay_reproduces_every_verdict_three_times(host: str, limit: int, tmp_path: Path) -> None:
    spec = HOSTS[host]
    recording = rp.record(spec, split="train", seed=7, limit=limit)
    assert len(recording.verdicts) == limit
    services = {e.service for e in recording.cassette.exchanges}
    assert f"llm:{host}" in services  # model answers are in the cassette too
    path = recording.cassette.save(tmp_path / f"{host}.yaml")
    cassette = Cassette.load(path)
    assert cassette.sha256 == recording.cassette.sha256
    for seed in (1, 2, 3):
        assert rp.replay(spec, cassette, split="train", seed=seed, limit=limit) == (
            recording.verdicts
        )


def test_ledger_cassettes_keep_injected_429s_at_their_position() -> None:
    recording = rp.record(HOSTS["ledger"], split="train", seed=7, limit=20)
    statuses = [e.response["status"] for e in recording.cassette.exchanges if e.service == "fx"]
    assert 429 in statuses
    assert set(recording.verdicts.values()) == {"PASS"}  # the healthy host absorbs them


def test_a_request_the_cassette_never_saw_fails_the_task(tmp_path: Path) -> None:
    spec = HOSTS["ledger"]
    recording = rp.record(spec, split="train", seed=7, limit=3)
    verdicts = rp.replay(spec, recording.cassette, split="train", seed=1, limit=6)
    assert [verdicts[t] for t in sorted(verdicts)][3:] == ["FAIL:MODEL_HTTP_502"] * 3

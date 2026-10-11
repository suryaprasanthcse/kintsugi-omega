from __future__ import annotations

import json
import os
import random
import statistics
import threading
from pathlib import Path

from kintsugi.fleet.hosts import load_hosts
from kintsugi.fleet.runtime import Deployer, Runtime, arrivals
from tests.contract.scratch import git, make_repo

HOSTS = load_hosts(Path(__file__).resolve().parents[2])


def test_the_deploy_follows_main(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    deploy = Deployer(repo, tmp_path / "state" / "deploy", ref="main", fetch=False)
    first = deploy.sync()
    assert first == git(repo, "rev-parse", "main").strip()
    assert deploy.sync() is None  # nothing moved
    (repo / "fix.txt").write_text("merged\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "merge a fix")
    second = deploy.sync()
    assert second == git(repo, "rev-parse", "main").strip() != first
    assert (deploy.path / "fix.txt").read_text(encoding="utf-8") == "merged\n"
    assert deploy.history == [first, second]
    expected = [str(deploy.path / "hosts" / name) for name in ("hostkit", "ledger")]
    assert deploy.pythonpath(["hostkit", "ledger"]) == os.pathsep.join(expected)


def test_arrivals_follow_the_configured_rate() -> None:
    gap = arrivals(random.Random(1), rate_per_hour=40)
    mean = statistics.fmean(gap() for _ in range(4000))
    assert abs(mean - 90.0) < 5.0  # 40 tasks per hour: one every 90 s on average
    fast = arrivals(random.Random(1), rate_per_hour=40, scale=60)
    assert abs(statistics.fmean(fast() for _ in range(4000)) - 1.5) < 0.1


def test_a_short_accelerated_run_serves_live_traffic(tmp_path: Path) -> None:
    runtime = Runtime(Path("."), {"ledger": HOSTS["ledger"]}, seed=3, deployer=None,
                      state_dir=tmp_path, scale=360.0)  # fmt: skip
    runtime.run(duration_s=2.0)
    rows = [json.loads(x) for x in (tmp_path / "runtime" / "ledger.jsonl").read_text().splitlines()]
    assert runtime.counts["ledger"] == len(rows) > 0
    assert all(row["task"].startswith("ledger-live-") for row in rows)
    assert {row["verdict"] for row in rows} == {"PASS"}


def test_fleet_down_stops_a_running_fleet(tmp_path: Path) -> None:
    (tmp_path / "runtime").mkdir()
    runtime = Runtime(Path("."), {"ledger": HOSTS["ledger"]}, seed=4, deployer=None,
                      state_dir=tmp_path, scale=0.001)  # fmt: skip
    stop = threading.Event()
    worker = threading.Thread(target=runtime.run, kwargs={"stop": stop})
    worker.start()
    (tmp_path / "runtime" / "STOP").write_text("stop\n")
    worker.join(timeout=30)
    assert not worker.is_alive()

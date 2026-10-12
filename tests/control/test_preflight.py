"""`kintsugi t0 check` is read-only and honest: it never reports an unobservable item as done."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyrage

from kintsugi.control import preflight

REPO = Path(__file__).resolve().parents[2]


def _api(rulesets: list[dict[str, Any]]) -> Any:
    def call(path: str) -> Any:
        if path.endswith("/rulesets"):
            return [{"id": r["id"]} for r in rulesets]
        return next(r for r in rulesets if path.endswith(f"/{r['id']}"))

    return call


def _by_key(items: list[preflight.Item]) -> dict[str, preflight.Item]:
    return {i.key: i for i in items}


def test_today_lists_the_custody_steps_and_never_claims_the_runtime(tmp_path: Path) -> None:
    identity = tmp_path / "rehearsal.agekey"
    identity.write_text(str(pyrage.x25519.Identity.generate()) + "\n", encoding="utf-8")
    items = _by_key(preflight.check(REPO, github_repo="o/r", gh_api=None,
                                    rehearsal_identity=identity))  # fmt: skip
    assert items["baselines"].done
    assert items["holdout-sealed"].done
    for key in ("runtime", "tunnel-webhook", "background-chaos", "rulesets"):
        assert not items[key].done
        assert items[key].owner == "architect"
    assert not items["sabotage-sealed"].done


def test_the_rehearsal_recipient_means_the_holdout_still_needs_resealing(tmp_path: Path) -> None:
    identity = tmp_path / "rehearsal.agekey"
    assert preflight._recipient_of(identity) is None
    real = pyrage.x25519.Identity.generate()
    identity.write_text("# created: now\n" + str(real) + "\n", encoding="utf-8")
    assert preflight._recipient_of(identity) == str(real.to_public())


def test_rulesets_must_be_active_on_main() -> None:
    dev = {"id": 1, "enforcement": "active",
           "conditions": {"ref_name": {"include": ["refs/heads/rehearsal/x"]}}}  # fmt: skip
    assert not preflight._rulesets(_api([dev]), "o/r").done
    main = {"id": 2, "enforcement": "active",
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"]}}}  # fmt: skip
    found = preflight._rulesets(_api([dev, main]), "o/r")
    assert found.done
    assert "[2]" in found.detail
    evaluate = {**main, "enforcement": "evaluate"}
    assert not preflight._rulesets(_api([evaluate]), "o/r").done

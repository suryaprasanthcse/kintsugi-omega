"""Secret redaction rules, the pre-push scan and the C6 honeytoken check (with controls).

Fake secrets are assembled at runtime so no secret-shaped literal ever lands in the repository
(GitHub push protection would rightly object).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from kintsugi.proof import redaction as r
from kintsugi.proof.passphrase import new_honeytoken
from kintsugi.sandbox import proc
from tests.helpers.checkpoints import user_line, write_checkpoint

CKPT = "01M4MT4M6K467TEZ3R6J340DAB"
FAKE = {
    "anthropic_api_key": "sk-" + "ant-api03-" + "Z" * 40,
    "github_token": "gh" + "p_" + "A1" * 20,
    "aws_access_key": "AK" + "IA" + "ABCDEFGHIJKLMNOP",
    "age_secret_key": "AGE-SECRET-" + "KEY-1" + "Q" * 58,
    "private_key_block": "-----BEGIN " + "OPENSSH PRIVATE KEY-----",
    "neatlogs_api_key": "NEATLOGS" + "_API_KEY=" + "nl_" + "x" * 30,
    "neatlogs_access_token": "nl" + "sa_" + "Ab3-" * 10,
    "bearer_token": "Authorization: " + "Bearer " + "e" * 40,
}


@pytest.mark.parametrize("rule", sorted(FAKE))
def test_each_rule_catches_its_secret_without_echoing_it(rule: str) -> None:
    findings = r.scan_text(f"prefix {FAKE[rule]} suffix")
    assert rule in {f.rule for f in findings}
    for finding in findings:
        assert FAKE[rule] not in finding.excerpt


def test_redact_removes_secrets() -> None:
    text = "\n".join(FAKE.values())
    redacted = r.redact(text)
    assert r.scan_text(redacted) == []
    for secret in FAKE.values():
        assert secret not in redacted


def test_passphrase_nonces_and_honeytokens_are_never_flagged() -> None:
    for _ in range(200):
        assert r.scan_text(new_honeytoken(), include_probe=True) == []


def test_probe_is_a_rule_but_not_a_push_blocker_by_default() -> None:
    probe = "kintsugi-redaction-probe-" + "123456"
    assert r.scan_text(probe) == []
    assert [f.rule for f in r.scan_text(probe, include_probe=True)] == [r.PROBE_RULE]


def test_rules_are_re2_compatible() -> None:
    # Entire compiles them with Go RE2: no look-arounds, back-references or possessives.
    for pattern in r.ENTIRE_RULES.values():
        re.compile(pattern)
        assert not re.search(r"\(\?[=!<]|\\[1-9]|\+\+|\*\+", pattern), pattern


def test_entire_settings_merge_keeps_existing_keys() -> None:
    current = {"enabled": True, "redaction": {"custom_redactions": {"mine": "x+"}}}
    merged = r.entire_settings_with_rules(current)
    assert merged["enabled"] is True
    custom = merged["redaction"]["custom_redactions"]  # type: ignore[index]
    assert custom["mine"] == "x+"
    assert set(r.ENTIRE_RULES) <= set(custom)
    with pytest.raises(ValueError, match="map"):
        r.entire_settings_with_rules({"redaction": {"custom_redactions": ["list"]}})


def test_the_repository_settings_carry_every_rule() -> None:
    settings = json.loads(
        (Path(__file__).resolve().parents[2] / ".entire" / "settings.json").read_text("utf-8")
    )
    assert settings["redaction"]["custom_redactions"] == dict(r.ENTIRE_RULES)


# ---- checkpoint trees -----------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    if proc.which("git") is None:
        pytest.skip("git is not installed")
    path = tmp_path / "repo"
    proc.run(["git", "init", "-q", str(path)], timeout_s=30, check=True)
    return path


def test_pre_push_scan_blocks_a_leaked_secret(repo: Path) -> None:
    write_checkpoint(repo, CKPT, user_line("clean prompt"))
    r.assert_pushable(repo)
    leaked = user_line(f"oops {FAKE['anthropic_api_key']}")
    write_checkpoint(repo, "01M4MXCTAM5YSYDX4HXMJ5V7H4", leaked)
    with pytest.raises(r.SecretsFound) as info:
        r.assert_pushable(repo)
    [finding] = info.value.findings
    assert finding.rule == "anthropic_api_key"
    assert finding.where.endswith(":0/full.jsonl")


def test_c6_positive_and_negative_controls(repo: Path) -> None:
    planted, unplanted = new_honeytoken(), new_honeytoken()
    write_checkpoint(repo, CKPT, user_line(f"a session that saw {planted}"))
    hits = r.honeytoken_hits(repo, planted)
    assert hits == [(f"refs/entire/checkpoints/AB/{CKPT}", "0/full.jsonl")]
    assert r.honeytoken_hits(repo, unplanted) == []


def test_c6_with_no_checkpoints_is_empty(repo: Path) -> None:
    assert r.honeytoken_hits(repo, new_honeytoken()) == []

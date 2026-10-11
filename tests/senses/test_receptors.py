"""Receptors R-001…R-007, their provisioning, and the I7 completeness check."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from kintsugi.senses import detections as d
from kintsugi.senses.completeness import ReportedHit, completeness
from kintsugi.senses.neatlogs_api import AlertRule, Detection, Webhook
from kintsugi.senses.store import SeenHit

REPO = Path(__file__).resolve().parents[2]


def test_all_seven_receptors_load_with_their_providers() -> None:
    specs = {s.id: s for s in d.load_receptors()}
    assert sorted(specs) == [f"R-00{n}" for n in range(1, 8)]
    providers = {k: s.provider for k, s in specs.items()}
    assert providers == {"R-001": "neatlogs", "R-002": "omega", "R-003": "omega",
                         "R-004": "omega", "R-005": "neatlogs", "R-006": "neatlogs",
                         "R-007": "neatlogs"}  # fmt: skip
    assert specs["R-006"].detection is not None
    assert specs["R-006"].detection.type == "pii"


def test_the_plan_renders_one_receptor_per_host_and_validates_against_the_pin() -> None:
    rendered = d.plan()  # plan() validates each body against the pinned contract
    assert len(rendered.detections) == 4 * 3
    assert len(rendered.metrics) == 3 * 3
    names = {p.name for p in rendered.detections}
    assert "R-007 ledger v1" in names
    r007 = next(p for p in rendered.detections if p.name == "R-007 scout v1")
    assert r007.detection["config"]["input_pattern"] == '"task_id":"scout-'
    assert r007.detection["runOn"] == {"frequency": "span_name", "span_names": ["grader.verdict"]}
    # neatlogs posts every hit; the Sentinel pages on 3 within 10 minutes (A-38).
    assert r007.alert == {"name": "R-007 scout v1 per_event", "ruleType": "per_event",
                          "severity": "critical", "cooldownMinutes": 10,
                          "description": r007.alert["description"]}  # fmt: skip
    assert (r007.threshold_count, r007.window_minutes) == (3, 10)
    r001 = next(p for p in rendered.detections if p.name == "R-001 ledger v1")
    assert r001.detection["runOn"]["span_names"] == [
        "parse_invoice", "lookup_vendor", "fx_rate", "post_journal_entry", "reconcile_report"
    ]  # fmt: skip
    assert r001.threshold_count == 9
    r006 = {p.host: p.detection["config"]["entities"] for p in rendered.detections
            if p.receptor == "R-006"}  # fmt: skip
    assert r006["ledger"] == ["EMAIL_ADDRESS", "CREDIT_CARD"]  # PHONE_NUMBER hit healthy lines
    assert "PHONE_NUMBER" in r006["scout"]
    assert "R-006 ledger v2" in names
    r002 = {m.host: m.threshold for m in rendered.metrics if m.receptor == "R-002"}
    assert r002 == {"ledger": 54, "scout": 54, "triage": 10}


def test_r007_matches_fail_verdicts_of_its_own_host_only() -> None:
    r007 = next(s for s in d.load_receptors() if s.id == "R-007")
    assert r007.detection is not None
    output = re.compile(r007.detection.config["output_pattern"])
    assert output.search('"FAIL:DUPLICATE_ENTRY"')
    assert output.search("FAIL:WRONG_AMOUNT")
    assert not output.search('"PASS"')
    assert not output.search('"PASS: no FAIL: here"')
    ledger = d.plan().detections
    pattern = next(p for p in ledger if p.name == "R-007 ledger v1").detection["config"]
    assert re.search(pattern["input_pattern"], '{"task_id":"ledger-train-002","verdict":"FAIL:X"}')
    assert not re.search(pattern["input_pattern"], '{"task_id":"scout-train-002","verdict":"x"}')


def test_r001_matches_failure_payloads_and_not_healthy_ones() -> None:
    r001 = next(s for s in d.load_receptors() if s.id == "R-001")
    assert r001.detection is not None
    pattern = re.compile(r001.detection.config["output_pattern"])
    failures = [
        '{"status": "error", "detail": "x"}',
        "HTTP 429 Too Many Requests",
        "Traceback (most recent call last)",
        "HTTP500",
        "ValueError exception",
    ]
    for text in failures:
        assert pattern.search(text), text
    assert not pattern.search('{"rate": 1.0842, "currency": "EUR"}')


def test_host_parameters_agree_with_the_hosts_themselves() -> None:
    params = d.load_host_params()
    for host, values in params.items():
        registry = yaml.safe_load((REPO / "hosts" / host / "host.yaml").read_text("utf-8"))
        assert values["tool_spans"] == list(registry["tools"]), host
        agent = (REPO / "hosts" / host / host / "agent.py").read_text("utf-8")
        assert f'@span(kind="WORKFLOW", name="{values["workflow_span"]}")' in agent, host
        assert values["task_prefix"] == f"{host}-"
        assert values["max_tool_calls"] >= 10
    ledger = yaml.safe_load((REPO / "hosts" / "ledger" / "host.yaml").read_text("utf-8"))
    assert params["ledger"]["l_max_ms"] == ledger["slo"]["p95_s"] * 1000


def test_placeholders_keep_types_and_leave_regex_quantifiers_alone() -> None:
    params = {"tool_spans": ["a", "b"], "n": 9, "prefix": "ledger-"}
    assert d.substitute("{tool_spans}", params) == ["a", "b"]
    assert d.substitute("{n}", params) == 9
    assert d.substitute('"task_id":"{prefix}', params) == '"task_id":"ledger-'
    assert d.substitute(r"\d{3}-{missing}", params) == r"\d{3}-{missing}"
    assert d.substitute(["{prefix}", "x"], params) == ["ledger-", "x"]


# --------------------------------------------------------------------------- provisioner


class FakeApi:
    def __init__(self) -> None:
        self.groups: list[dict[str, Any]] = []
        self.dets: dict[str, Detection] = {}
        self.rules: dict[str, AlertRule] = {}
        self.hooks: dict[str, Webhook] = {}
        self.calls: list[str] = []
        self.keys: list[str | None] = []

    def _stamp(self) -> datetime:
        return datetime(2026, 10, 11, tzinfo=UTC)

    def detection_groups(self) -> list[dict[str, Any]]:
        return self.groups

    def create_detection_group(self, name: str, *, idempotency_key: str | None = None) -> dict:
        self.calls.append("group")
        group = {"id": "00000000-0000-4000-8000-000000000001", "name": name}
        self.groups.append(group)
        return group

    def detections(self, *, group_id: str | None = None) -> list[Detection]:
        return list(self.dets.values())

    def create_detection(self, body: dict, *, idempotency_key: str | None = None) -> Detection:
        self.calls.append(f"det {body['displayName']}")
        self.keys.append(idempotency_key)
        det = Detection(
            id=f"det-{len(self.dets)}",
            display_name=body["displayName"],
            slug="s",
            type=body["type"],
            is_active=True,
            group_id=body["groupId"],
            threshold=0.5,
            created_at=self._stamp(),
            updated_at=self._stamp(),
        )
        self.dets[det.id] = det
        return det

    def set_detection_status(self, detection_id: str, active: bool, **_: Any) -> Detection:
        self.calls.append(f"status {detection_id} {active}")
        det = self.dets[detection_id].model_copy(update={"is_active": active})
        self.dets[detection_id] = det
        return det

    def alert_rules(self) -> list[AlertRule]:
        return list(self.rules.values())

    def create_alert_rule(self, body: dict, *, idempotency_key: str | None = None) -> AlertRule:
        self.calls.append(f"rule {body['name']}")
        assert body["channelMask"]["webhookIds"] == ["hook-1"]
        rule = AlertRule(id=f"rule-{len(self.rules)}", name=body["name"],
                         rule_type=body["ruleType"], enabled=True, severity=body["severity"],
                         detection_id=body["detectionId"])  # fmt: skip
        self.rules[rule.id] = rule
        return rule

    def set_alert_rule_status(self, rule_id: str, enabled: bool, **_: Any) -> AlertRule:
        self.calls.append(f"rule status {self.rules[rule_id].name} {enabled}")
        rule = self.rules[rule_id].model_copy(update={"enabled": enabled})
        self.rules[rule_id] = rule
        return rule

    def webhooks(self) -> list[Webhook]:
        return list(self.hooks.values())

    def create_webhook(self, name: str, url: str, events: Any, **_: Any) -> Webhook:
        self.calls.append("webhook")
        hook = Webhook(id="hook-1", name=name, enabled=True, event_types=tuple(events),
                       destination_configured=True, created_at=self._stamp(),
                       updated_at=self._stamp())  # fmt: skip
        self.hooks[hook.id] = hook
        return hook

    def update_webhook(self, webhook_id: str, body: dict, **_: Any) -> Webhook:
        self.calls.append("webhook update")
        return self.hooks[webhook_id]

    def set_webhook_status(self, webhook_id: str, enabled: bool, **_: Any) -> Webhook:
        return self.hooks[webhook_id]


def test_provisioning_is_idempotent_and_retires_stale_versions(tmp_path: Path) -> None:
    api = FakeApi()
    first = d.Provisioner(api)
    hook = first.webhook("https://example.invalid/hook/" + "x" * 43, "secret" * 8)
    manifest = first.ensure(webhook_id=hook)
    assert len(manifest.detections) == 12
    assert len(api.dets) == 12
    assert len(api.rules) == 12
    assert {b.receptor for b in manifest.detections.values()} == {"R-001", "R-005", "R-006",
                                                                 "R-007"}  # fmt: skip
    assert all(key and len(key) == 43 for key in api.keys)
    path = manifest.save(tmp_path / "receptors.json")
    assert d.Manifest.load(path).detections == manifest.detections

    api.calls.clear()
    second = d.Provisioner(api)
    second.webhook("https://example.invalid/hook/" + "x" * 43, "secret" * 8)
    again = second.ensure(webhook_id="hook-1")
    assert api.calls == ["webhook update"]  # nothing re-created
    assert again.detections.keys() == manifest.detections.keys()

    specs = d.load_receptors()
    bumped = [s.model_copy(update={"version": 9}) if s.id == "R-005" else s for s in specs]
    third = d.Provisioner(api)
    third.ensure(d.plan(bumped, group_id="00000000-0000-4000-8000-000000000001"),
                 webhook_id="hook-1")  # fmt: skip
    retired = {"R-005 ledger v1", "R-005 scout v1", "R-005 triage v1"}
    assert set(third.deactivated) == retired | {f"{n} per_event" for n in retired}
    assert "R-005 ledger v9" in third.created
    enabled = {r.name for r in api.rules.values() if r.enabled}
    assert not {f"{n} per_event" for n in retired} & enabled


def test_a_superseded_rule_type_is_retired_and_replaced() -> None:
    api = FakeApi()
    group = "00000000-0000-4000-8000-000000000001"
    d.Provisioner(api).ensure(webhook_id="hook-1")
    det = next(x for x in api.dets.values() if x.display_name == "R-007 ledger v1")
    old = AlertRule(id="rule-old", name="R-007 ledger v1 alert", rule_type="threshold",
                    enabled=True, severity="critical", detection_id=det.id)  # fmt: skip
    api.rules[old.id] = old
    again = d.Provisioner(api)
    manifest = again.ensure(d.plan(group_id=group), webhook_id="hook-1")
    assert "R-007 ledger v1 alert" in again.deactivated
    assert not api.rules["rule-old"].enabled
    assert manifest.detections[det.id].alert_rule_id != "rule-old"
    assert manifest.detections[det.id].threshold_count == 3


# --------------------------------------------------------------------------- I7


def seen(key: str, incident: str | None, disposition: str | None) -> SeenHit:
    return SeenHit(key, None, "d", "t", "s", "poller", "R-007", "ledger", None, incident,
                   disposition)  # fmt: skip


def test_i7_passes_when_every_hit_in_the_window_maps_to_an_incident_or_dedupe() -> None:
    start = datetime(2026, 10, 11, tzinfo=UTC)
    end = start + timedelta(hours=24)
    hits = [ReportedHit("d", f"t{i}", "s", start + timedelta(minutes=i)) for i in range(6)]
    rows = {h.key: seen(h.key, "INC-1" if i < 3 else "INC-2", "opened" if i in (0, 3) else
                        "attached") for i, h in enumerate(hits)}  # fmt: skip
    outside = ReportedHit("d", "late", "s", end + timedelta(minutes=1))
    report = completeness([*hits, outside, hits[0]], rows, start, end)
    assert report.ok
    assert (report.total, len(report.incidents), report.opened, report.attached) == (6, 2, 2, 4)
    assert report.summary().startswith("6 hits -> 2 incidents + 4 dedupes")


def test_i7_fails_on_a_missing_or_undecided_hit() -> None:
    start = datetime(2026, 10, 11, tzinfo=UTC)
    end = start + timedelta(hours=1)
    a = ReportedHit("d", "a", "s", start)
    b = ReportedHit("d", "b", "s", start)
    c = ReportedHit("d", "c", "s", start)
    rows = {a.key: seen(a.key, "INC-1", "opened"), b.key: seen(b.key, None, None)}
    report = completeness([a, b, c], rows, start, end)
    assert not report.ok
    assert report.pending == [b.key]
    assert report.missing == [c.key]


@pytest.mark.parametrize("bad", ["R-009", "R-00x"])
def test_a_receptor_file_must_name_itself(tmp_path: Path, bad: str) -> None:
    source = (d.RECEPTOR_DIR / "R-007.yaml").read_text("utf-8")
    (tmp_path / f"{bad}.yaml").write_text(source, encoding="utf-8")
    with pytest.raises(d.ReceptorError):
        d.load_receptors(tmp_path)

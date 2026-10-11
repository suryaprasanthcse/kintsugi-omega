"""Innate receptors R-001…R-007 and their provisioning (spec §4.1.4, §4.1.5).

Receptors are versioned specs in ``receptors/R-00N.yaml`` with per-host parameters in
``receptors/hosts.yaml``. Two providers:

- ``neatlogs``: a neatlogs detection (regex, PII or classifier), scoped to one host by span name
  or by the verdict's task prefix, plus a threshold alert rule that posts to Omega's webhook;
- ``omega``: a metric over trace summaries (tool calls, cost, latency) that the poller
  evaluates, because neatlogs conditional detections never fired in the Stage 4 spike.

``Provisioner.ensure`` is idempotent: detections and rules are found by their deterministic
display names (``R-007 ledger v1``) inside the ``kintsugi-receptors`` group, created only when
missing, with idempotency keys derived from their content, and stale versions are deactivated.
The resulting ids are written to a manifest the ingress and the poller read. Verified live in
Stage 4: groups, regex/PII/classifier detections, threshold and per-event alert rules and
webhooks are all created with a service-account token (spike A13).
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict

from kintsugi.senses import contract

RECEPTOR_DIR = Path(__file__).with_name("receptors")
GROUP_NAME = "kintsugi-receptors"
WEBHOOK_NAME = "kintsugi-ingress"
EVENT_TYPES = (
    "alert.per_event_match",
    "alert.threshold_breach",
    "alert.metric_threshold_breach",
    "alert.anomaly_detected",
    "alert.compound_breach",
    "alert.sla_breach_warning",
    "alert.ingestion_silent",
)
_PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


class ReceptorError(ValueError):
    pass


class _Spec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DetectionSpec(_Spec):
    type: Literal["regex", "pii", "classifier"]
    run_on: dict[str, Any]
    config: dict[str, Any]
    threshold: float | None = None


class MetricSpec(_Spec):
    field: Literal["tool_calls", "cost_usd", "duration_ms"]
    operator: Literal[">", ">="]
    threshold: float | str


class AlertSpec(_Spec):
    rule_type: Literal["threshold", "per_event"]
    threshold_count: int | str = 3
    window_minutes: int = 10
    severity: Literal["critical", "warning", "info"] = "warning"
    cooldown_minutes: int = 10


class ReceptorSpec(_Spec):
    id: str
    version: int
    title: str
    catches: str
    provider: Literal["neatlogs", "omega"]
    detection: DetectionSpec | None = None
    metric: MetricSpec | None = None
    alert: AlertSpec
    notes: str | None = None

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(), sort_keys=True).encode()).hexdigest()


def load_receptors(directory: Path = RECEPTOR_DIR) -> list[ReceptorSpec]:
    specs = []
    for path in sorted(directory.glob("R-*.yaml")):
        spec = ReceptorSpec.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        if spec.id != path.stem:
            raise ReceptorError(f"{path.name} declares {spec.id}")
        if (spec.provider == "neatlogs") != (spec.detection is not None):
            raise ReceptorError(f"{spec.id}: a neatlogs receptor needs a detection, omega a metric")
        if spec.provider == "omega" and spec.metric is None:
            raise ReceptorError(f"{spec.id}: an omega receptor needs a metric")
        specs.append(spec)
    return specs


def load_host_params(directory: Path = RECEPTOR_DIR) -> dict[str, dict[str, Any]]:
    data = yaml.safe_load((directory / "hosts.yaml").read_text(encoding="utf-8"))
    return {host: dict(params) for host, params in data["hosts"].items()}


def substitute(value: Any, params: Mapping[str, Any]) -> Any:
    """Fill ``{name}`` placeholders; a value that is only a placeholder keeps the param's type."""
    if isinstance(value, str):
        whole = _PLACEHOLDER.fullmatch(value)
        if whole and whole.group(1) in params:
            return params[whole.group(1)]
        return _PLACEHOLDER.sub(lambda m: str(params.get(m.group(1), m.group(0))), value)
    if isinstance(value, list):
        out: list[Any] = []
        for item in value:
            filled = substitute(item, params)
            out.extend(filled if isinstance(filled, list) and isinstance(item, str) else [filled])
        return out
    if isinstance(value, dict):
        return {k: substitute(v, params) for k, v in value.items()}
    return value


def display_name(spec: ReceptorSpec, host: str) -> str:
    return f"{spec.id} {host} v{spec.version}"


def content_key(*parts: Any) -> str:
    """A deterministic idempotency key (unpadded base64url SHA-256, accepted by the API)."""
    material = json.dumps(parts, sort_keys=True, default=str).encode("utf-8")
    return base64.urlsafe_b64encode(hashlib.sha256(material).digest()).rstrip(b"=").decode()


@dataclass(frozen=True)
class MetricReceptor:
    receptor: str
    host: str
    version: int
    field: str
    operator: str
    threshold: float

    @property
    def detection_id(self) -> str:
        """Hits of an Omega receptor are keyed under this pseudo detection id."""
        return f"omega:{self.receptor}:{self.host}:v{self.version}"

    def fires(self, value: float) -> bool:
        return value > self.threshold if self.operator == ">" else value >= self.threshold


@dataclass(frozen=True)
class Planned:
    receptor: str
    host: str
    version: int
    name: str
    detection: dict[str, Any]
    alert: dict[str, Any]
    digest: str
    threshold_count: int = 3  # the Sentinel's paging rule: this many hits ...
    window_minutes: int = 10  # ... within this window, per host (§4.1.4)


@dataclass
class Plan:
    detections: list[Planned] = field(default_factory=list)
    metrics: list[MetricReceptor] = field(default_factory=list)


def plan(
    specs: Sequence[ReceptorSpec] | None = None,
    hosts: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    group_id: str = "00000000-0000-0000-0000-000000000000",
) -> Plan:
    """Render every receptor for every host into pinned API bodies (validated against the pin)."""
    specs = load_receptors() if specs is None else specs
    hosts = load_host_params() if hosts is None else hosts
    result = Plan()
    for spec in specs:
        for host, params in sorted(hosts.items()):
            values = {"host": host, **params}
            name = display_name(spec, host)
            if spec.provider == "omega":
                assert spec.metric is not None
                threshold = float(substitute(spec.metric.threshold, values))
                result.metrics.append(MetricReceptor(spec.id, host, spec.version,
                                                     spec.metric.field, spec.metric.operator,
                                                     threshold))  # fmt: skip
                continue
            assert spec.detection is not None
            detection: dict[str, Any] = {
                "type": spec.detection.type,
                "displayName": name,
                "description": f"{spec.title}: {spec.catches} (KINTSUGI-Ω receptor {spec.id} "
                f"v{spec.version} for host:{host}; spec sha256 {spec.digest()[:12]})",
                "groupId": group_id,
                "isActive": True,
                "runOn": _run_on(substitute(spec.detection.run_on, values)),
                "config": substitute(spec.detection.config, values),
            }
            if spec.detection.threshold is not None:
                detection["threshold"] = spec.detection.threshold
            contract.validate_request("detections.create", detection)
            count = int(substitute(spec.alert.threshold_count, values))
            alert = {
                "name": f"{name} {spec.alert.rule_type}",
                "ruleType": spec.alert.rule_type,
                "severity": spec.alert.severity,
                "cooldownMinutes": spec.alert.cooldown_minutes,
                "description": f"{spec.id} for host:{host}: {spec.catches}",
            }
            if spec.alert.rule_type == "threshold":
                alert["thresholdCount"] = count
                alert["windowMinutes"] = spec.alert.window_minutes
            result.detections.append(Planned(spec.id, host, spec.version, name, detection, alert,
                                             spec.digest(), count,
                                             spec.alert.window_minutes))  # fmt: skip
    return result


def _run_on(run_on: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"frequency": run_on["frequency"]}
    for key in ("span_names", "span_types"):
        if key in run_on:
            out[key] = list(run_on[key])
    return out


# --------------------------------------------------------------------------- provisioning


@dataclass(frozen=True)
class Binding:
    """What a detection id means to Omega (read by the ingress and the poller)."""

    detection_id: str
    receptor: str
    host: str
    version: int
    alert_rule_id: str | None = None
    threshold_count: int = 3
    window_minutes: int = 10


@dataclass
class Manifest:
    group_id: str
    webhook_id: str | None
    detections: dict[str, Binding]
    metrics: list[MetricReceptor]
    provisioned_at: str

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "group_id": self.group_id,
            "webhook_id": self.webhook_id,
            "provisioned_at": self.provisioned_at,
            "detections": {k: vars(v) for k, v in sorted(self.detections.items())},
            "metrics": [vars(m) for m in self.metrics],
        }
        path.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8", newline="\n")
        return path

    @classmethod
    def load(cls, path: Path) -> Manifest:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            group_id=data["group_id"],
            webhook_id=data.get("webhook_id"),
            detections={k: Binding(**v) for k, v in data["detections"].items()},
            metrics=[MetricReceptor(**m) for m in data["metrics"]],
            provisioned_at=data["provisioned_at"],
        )


class Provisioner:
    def __init__(self, api: Any) -> None:
        self.api = api
        self.created: list[str] = []
        self.deactivated: list[str] = []

    def group(self, name: str = GROUP_NAME) -> str:
        for group in self.api.detection_groups():
            if group.get("name") == name:
                return group["id"]
        made = self.api.create_detection_group(name, idempotency_key=content_key("group", name))
        self.created.append(f"group {name}")
        return made["id"]

    def webhook(self, url: str, secret: str, *, name: str = WEBHOOK_NAME) -> str:
        """Create or repoint the ingress webhook (the destination URL is redacted on read)."""
        body = {"destinationUrl": url, "secret": secret, "eventTypes": list(EVENT_TYPES)}
        for hook in self.api.webhooks():
            if hook.name == name:
                self.api.update_webhook(hook.id, body, idempotency_key=content_key("wh", body))
                if not hook.enabled:
                    self.api.set_webhook_status(hook.id, True)
                return hook.id
        key = content_key("wh-new", name, body)
        made = self.api.create_webhook(name, url, EVENT_TYPES, secret=secret, idempotency_key=key)
        self.created.append(f"webhook {name}")
        return made.id

    def _retire_rules(self, rules: Any, detection_id: str, *, keep: str | None) -> None:
        """Disable a detection's other enabled rules (a superseded rule type or version)."""
        for rule in list(rules):
            if rule.detection_id == detection_id and rule.id != keep and rule.enabled:
                self.api.set_alert_rule_status(rule.id, False)
                self.deactivated.append(rule.name)

    def ensure(self, desired: Plan | None = None, *, webhook_id: str | None = None) -> Manifest:
        group_id = self.group()
        desired = desired or plan(group_id=group_id)
        existing = {d.display_name: d for d in self.api.detections(group_id=group_id)}
        rules = {r.name: r for r in self.api.alert_rules()}
        bindings: dict[str, Binding] = {}
        wanted = {p.name for p in desired.detections}
        for item in desired.detections:
            body = {**item.detection, "groupId": group_id}
            found = existing.get(item.name)
            if found is None:
                found = self.api.create_detection(body, idempotency_key=content_key("det", body))
                self.created.append(item.name)
            elif not found.is_active:
                self.api.set_detection_status(found.id, True)
            alert = {**item.alert, "detectionId": found.id}
            if webhook_id:
                alert["channelMask"] = {"email": False, "slack": False, "webhookIds": [webhook_id]}
            rule = rules.get(alert["name"])
            if rule is None:
                rule = self.api.create_alert_rule(alert, idempotency_key=content_key("rule", alert))
                self.created.append(alert["name"])
            self._retire_rules(rules.values(), found.id, keep=rule.id)
            bindings[found.id] = Binding(found.id, item.receptor, item.host, item.version, rule.id,
                                         item.threshold_count, item.window_minutes)  # fmt: skip
        for name, stale in existing.items():
            if name not in wanted:
                if stale.is_active:
                    self.api.set_detection_status(stale.id, False)
                    self.deactivated.append(name)
                self._retire_rules(rules.values(), stale.id, keep=None)
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return Manifest(group_id, webhook_id, bindings, list(desired.metrics), stamp)

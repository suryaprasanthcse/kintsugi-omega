"""The Sentinel: hits in, deduplicated and severity-ranked incidents out (spec §5.7, §6.2 step 6).

For each hit the FleetSupervisor routes (``route_hit`` activity):

1. **Attach** it to an open incident on the same host: one failure, one incident.
2. Otherwise apply the receptor's **paging rule** (amendment A-38: neatlogs alert rules are
   per-event hints, so the "≥ N hits in W minutes per host" rule is the Sentinel's): count this
   receptor's claimed hits on this host within the window. At the threshold, **open** an incident
   and attach the window's earlier held hits to it.
3. Below the threshold the hit is **held** against the receptor's watch record (A-47).

Every hit gets exactly one disposition in the senses store (``SenseStore.assign``), which is what
invariant I7 checks. Opening writes ``IncidentOpen{host, prelim_signature, hits[], severity,
var_estimate}`` to the case file. Severity comes from ``claude-haiku-5-5`` (4k-token budget,
hits and trace summaries only, never code), with a deterministic VaR rule as the fallback.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from kintsugi.control import contracts as c
from kintsugi.crew.consult import consult
from kintsugi.crew.context import CrewContext
from kintsugi.crew.manifest import ContextBuilder
from kintsugi.econ import governor
from kintsugi.proof.canonical import canonicalize, sha256_hex
from kintsugi.senses.detections import Binding

SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "severity": {"type": "string", "enum": ["low", "warning", "high"]},
        "summary": {"type": "string", "maxLength": 400},
    },
    "required": ["severity", "summary"],
    "additionalProperties": False,
}
DEFAULT_BINDING = Binding("", "unknown", "unknown", 1)


def _when(text: str | None, fallback: datetime) -> datetime:
    if not text:
        return fallback
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return fallback
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def watch_record(receptor: str, host: str) -> str:
    return f"WATCH-{receptor}-{host}"


def prelim_signature(host: str, receptor: str) -> str:
    return "sha256:" + sha256_hex(canonicalize({"host": host, "receptor": receptor}))


class Sentinel:
    def __init__(self, ctx: CrewContext) -> None:
        if ctx.store is None:
            raise ValueError("the Sentinel needs the senses store (I7 dispositions)")
        self.ctx = ctx
        self.store = ctx.store

    def binding(self, hit: c.Hit) -> Binding:
        bound = self.ctx.bindings.get(hit.detection_id)
        if bound is not None:
            return bound
        return Binding(
            hit.detection_id,
            hit.receptor or DEFAULT_BINDING.receptor,
            hit.host or DEFAULT_BINDING.host,
            1,
        )

    def route(self, hit: c.Hit, open_incidents: list[str]) -> c.RouteDecision:
        bound = self.binding(hit)
        host, receptor = hit.host or bound.host, hit.receptor or bound.receptor
        index = self.ctx.incidents
        for incident in open_incidents:
            if index.host_of(incident) == host:
                self.store.assign(hit.hit_key, incident, "attached")
                return c.RouteDecision("attach", incident, reason=f"open incident on {host}")
        now = datetime.fromtimestamp(self.ctx.clock(), UTC)
        at = _when(hit.detected_at, now)
        since = at - timedelta(minutes=bound.window_minutes)
        window = [
            h
            for h in self.store.hits_for(receptor, host)
            if since <= _when(h.detected_at, now) <= at
        ]
        if hit.hit_key not in {h.hit_key for h in window}:
            window.append(self.store.seen(hit.hit_key) or _stub(hit))
        rule = f">= {bound.threshold_count} hits in {bound.window_minutes} min on {host}"
        if len(window) < bound.threshold_count:
            self.store.assign(hit.hit_key, watch_record(receptor, host), "held")
            return c.RouteDecision("drop", reason=f"held: {len(window)} of {rule}")
        incident = index.open(host, receptor)
        for other in window:
            if other.hit_key != hit.hit_key and other.disposition in (None, "held"):
                self.store.assign(other.hit_key, incident, "attached")
        self.store.assign(hit.hit_key, incident, "opened")
        severity = self._open(incident, host, receptor, rule, window)
        return c.RouteDecision("open", incident, severity, f"paged: {len(window)} hits, {rule}")

    def _var(self, host: str, hits: int, window_minutes: int) -> float:
        target = self.ctx.targets.get(host)
        if target is None:
            return 0.0
        econ = target.host.economics
        expected = max(1e-9, econ.tasks_per_hour * window_minutes / 60.0)
        r_fail = min(1.0, hits / expected)
        return governor.value_at_risk(
            r_fail, econ.tasks_per_hour, econ.value_per_failed_task_usd, 24.0
        )

    def _open(self, incident: str, host: str, receptor: str, rule: str, window: list) -> str:
        bound_window = self.ctx.bindings.get(window[0].detection_id) if window else None
        minutes = bound_window.window_minutes if bound_window else 10
        var = round(self._var(host, len(window), minutes), 2)
        case = self.ctx.case(incident)
        builder = ContextBuilder("sentinel", incident)
        builder.bind("host", host)
        builder.bind("receptor", receptor)
        builder.bind("rule", rule)
        builder.bind("var_usd", f"{var:.2f}")
        listing = "\n".join(
            f"- {h.detected_at} {h.detection_id} trace {h.trace_id}" for h in window[-20:]
        )
        builder.add("hits", listing, source="claimed hits (senses store)", why="the alert")
        builder.exclude("source code", "the Sentinel never reads code (§5.7)")

        def fallback() -> dict[str, str]:
            level = "high" if var >= 100 else ("warning" if var >= 10 else "low")
            return {
                "severity": level,
                "summary": f"{len(window)} {receptor} hits on {host} within the window",
            }

        answer = consult(
            self.ctx.model,
            case,
            builder,
            "sentinel",
            schema=SCHEMA,
            fallback=fallback,
            max_budget_usd=0.25,
            model_id=self.ctx.model_for("sentinel"),
        )
        severity = str(answer.data["severity"])
        case.put(
            "incident_open",
            "open",
            {
                "host": host,
                "receptor": receptor,
                "prelim_signature": prelim_signature(host, receptor),
                "hits": [h.hit_key for h in window],
                "severity": severity,
                "var_estimate": var,
                "summary": answer.data["summary"],
                "rule": rule,
            },
            by="sentinel",
        )
        return severity


def _stub(hit: c.Hit) -> Any:
    from kintsugi.senses.store import SeenHit

    return SeenHit(
        hit.hit_key,
        hit.hit_id,
        hit.detection_id,
        hit.trace_id,
        hit.span_id,
        hit.source,
        hit.receptor,
        hit.host,
        hit.detected_at,
        None,
        None,
    )

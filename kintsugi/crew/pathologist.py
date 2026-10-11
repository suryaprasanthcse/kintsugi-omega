"""The Pathologist: confirm, cluster, describe and reproduce (spec §5.7, §6.2 step 7).

``triage`` turns an alert into a :class:`~kintsugi.control.contracts.Signature`: every failing
trace becomes an observation, observations cluster by the deterministic signature φ, and the
dominant cluster names the incident. ``claude-opus-5-5`` labels it and writes the symptom
summary (≤ 800 characters) from the five most representative traces and the cluster statistics
only. The Pathologist never reads source code, so the symptom is not anchored on a guess.

``reproduce`` is gate G0: the dominant cluster's failing tasks are re-run at their own seed on
the incident's base commit, recording the cassette that must fail 3/3 when replayed. If they do
not reproduce, the task set is widened ×3 once; still nothing means PARK (§5.11).
"""

from __future__ import annotations

import importlib
from collections import Counter
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from kintsugi.control import contracts as c
from kintsugi.control.activities import InvalidAlert
from kintsugi.crew.consult import consult
from kintsugi.crew.context import CrewContext
from kintsugi.crew.manifest import ContextBuilder
from kintsugi.gates import g0
from kintsugi.gates.checkout import clean_checkout, rev
from kintsugi.sandbox import chaos_proxy as chaos
from kintsugi.senses.signature import FailureObservation, cluster, representatives

SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "maxLength": 80},
        "summary": {"type": "string", "maxLength": 800},
    },
    "required": ["label", "summary"],
    "additionalProperties": False,
}
REPRO_TASKS = 3


def _describe(obs: FailureObservation) -> str:
    gram = " -> ".join(obs.preceding) or "(no tool calls)"
    return (
        f"- trace {obs.trace_id}: {obs.verdict or obs.error} after [{gram}] "
        f"(failing span {obs.failing_kind} {obs.failing_name}, tool {obs.tool_name})"
    )


def deterministic_summary(host: str, groups: list, dominant: Any) -> dict[str, str]:
    fields = dominant.fields
    gram = " -> ".join(fields["preceding"]) or "no tool calls"
    share = f"{dominant.size}/{sum(g.size for g in groups)}"
    summary = (
        f"On {host}, {share} failing traces end in {fields['error_class']} on "
        f"{fields['failing_span'][1]} after the tool sequence [{gram}]; the last tool "
        f"called is {fields['tool'] or 'none'}."
    )
    return {"label": f"{fields['error_class']} after {gram}"[:80], "summary": summary[:800]}


class Pathologist:
    def __init__(self, ctx: CrewContext) -> None:
        if ctx.traces is None:
            raise ValueError("the Pathologist needs a trace source")
        self.ctx = ctx

    def _with_assigned_hits(self, alert: c.Alert) -> c.Alert:
        """The opening alert carries only the hit that crossed the paging threshold; the
        window's earlier hits were attached to the incident in the senses store (I7)."""
        if self.ctx.store is None:
            return alert
        known = {h.hit_key for h in alert.hits}
        more = tuple(
            c.Hit(
                s.hit_key,
                s.detection_id,
                s.trace_id,
                s.span_id,
                s.source,
                s.receptor,
                s.host,
                s.detected_at,
                s.hit_id,
            )
            for s in self.ctx.store.seen_hits().values()
            if s.incident == alert.incident_id and s.hit_key not in known
        )
        return replace(alert, hits=alert.hits + more) if more else alert

    def triage(self, alert: c.Alert) -> c.Signature:
        alert = self._with_assigned_hits(alert)
        observations = self.ctx.traces.observations(alert) if self.ctx.traces else []
        if not observations:
            raise InvalidAlert(f"{alert.incident_id}: no failing trace behind the alert")
        groups = cluster(observations)
        dominant = groups[0]
        case = self.ctx.case(alert.incident_id)
        case.put(
            "observations",
            f"pass{alert.reentries}",
            {
                "clusters": [
                    {
                        "phi": g.phi,
                        "size": g.size,
                        "fields": g.fields,
                        "members": [vars(m) for m in g.members],
                    }
                    for g in groups
                ]
            },
            by="pathologist",
        )
        builder = ContextBuilder("pathologist", alert.incident_id)
        builder.bind("incident", alert.incident_id)
        builder.bind("host", alert.host)
        stats = "\n".join(
            f"- {g.phi[:19]}: {g.size} traces, hosts {g.hosts}, {g.fields}" for g in groups[:5]
        )
        builder.add("clusters", stats, source="signature clusters", why="cluster statistics")
        reps = representatives(dominant, 5)
        builder.add(
            "observations",
            "\n".join(_describe(o) for o in reps),
            source="5 representative failing traces",
            why="symptom evidence",
        )
        builder.exclude("source code", "never read by the Pathologist (§5.7)")
        builder.exclude("holdout tasks", "invariant I5")
        answer = consult(
            self.ctx.model,
            case,
            builder,
            "pathologist",
            schema=SCHEMA,
            fallback=lambda: deterministic_summary(alert.host, groups, dominant),
            max_budget_usd=1.5,
            model_id=self.ctx.model_for("pathologist"),
        )
        summary = str(answer.data["summary"])[:800]
        case.put(
            "signature",
            "phi",
            {
                "phi": dominant.phi,
                "fields": dominant.fields,
                "size": dominant.size,
                "clusters": len(groups),
                "label": answer.data["label"],
                "trace_ids": [m.trace_id for m in dominant.members][:20],  # the receipt links
            },
            by="pathologist",
        )
        case.put(
            "symptom",
            "summary",
            {"summary": summary, "label": answer.data["label"]},
            by="pathologist",
        )
        return c.Signature(
            alert.incident_id,
            alert.host,
            dominant.phi,
            tuple(m.trace_id for m in dominant.members),
            alert.severity,
            summary,
            alert.reentries,
        )

    # ---- G0 -------------------------------------------------------------------------------------

    def _dominant_members(self, sig: c.Signature) -> list[dict[str, Any]]:
        view = self.ctx.case(sig.incident_id).reader("pathologist")
        data = view.get("observations", f"pass{sig.pass_no}")
        group = next(g for g in data["clusters"] if g["phi"] == sig.phi)
        return [m for m in group["members"] if m.get("repro")]

    def reproduce(self, sig: c.Signature) -> c.Repro:
        target = self.ctx.target(sig.host)
        members = self._dominant_members(sig)
        usable = [m for m in members if m["repro"]["split"] != "holdout"]  # I5: never holdout
        case = self.ctx.case(sig.incident_id)
        if not usable:
            case.put(
                "repro", "g0", {"ok": False, "reason": "no replayable trace"}, by="pathologist"
            )
            return c.Repro(False, None, 0, 0)
        seeds = Counter((m["repro"]["split"], int(m["repro"]["seed"])) for m in usable)
        (split, seed), _ = seeds.most_common(1)[0]
        wanted = sorted(
            {
                m["repro"]["task_id"]
                for m in usable
                if (m["repro"]["split"], int(m["repro"]["seed"])) == (split, seed)
            }
        )
        target.make_importable()
        generator = importlib.import_module(f"{target.host.package}.evals.generate")
        pool = {t["id"]: t for t in generator.tasks("train")}
        base = rev(target.repo, self.ctx.base_ref)
        profile = chaos.background_profile()
        attempts = []
        result = None
        for size in (REPRO_TASKS, REPRO_TASKS * 3):  # widen ×3 once (§5.11)
            tasks = [pool[t] for t in wanted[:size] if t in pool]
            if not tasks:
                break
            with clean_checkout(target.repo, base) as checkout:
                result = g0.reproduce(
                    target, checkout, tasks, seed=seed, split=split, profile=profile
                )
            attempts.append(result.result.as_dict())
            if result.ok:
                break
        ok = bool(result and result.ok and result.cassette is not None)
        if result is not None and result.cassette is not None:
            case.put("cassette", "g0", result.cassette.as_dict(), by="pathologist")
        case.put(
            "repro",
            "g0",
            {
                "ok": ok,
                "base_sha": base,
                "seed": seed,
                "split": split,
                "profile": profile.name,
                "tasks": [t for t in wanted[: REPRO_TASKS * 3] if t in pool],
                "failing": result.failing if result else [],
                "attempts": attempts,
                "cassette_sha": result.cassette.sha256 if result and result.cassette else None,
            },
            by="pathologist",
        )
        failures = int(result.result.evidence.get("replays_failing", 0)) if result else 0
        return c.Repro(
            ok,
            result.cassette.sha256 if ok and result and result.cassette else None,
            failures,
            g0.REPLAYS,
        )

"""Live neatlogs Public API contract tests (Stage 4; run with KINTSUGI_CONTRACT=1).

They need ``NEATLOGS_PROJECT_ID`` and ``NEATLOGS_SERVICE_TOKEN`` in ``.env`` and network access.
The published OpenAPI must still match the pin, live responses must validate against it, and the
hosts' ``grader.verdict`` spans must be readable through the API (the first Stage 4 target).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from kintsugi.fleet.fabric import load_dotenv
from kintsugi.senses import contract
from kintsugi.senses.neatlogs_api import Credentials, NeatlogsApi
from kintsugi.senses.records import load_trace

pytestmark = pytest.mark.contract
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def api() -> Iterator[NeatlogsApi]:
    env = load_dotenv(REPO / ".env")
    if not env.get("NEATLOGS_SERVICE_TOKEN") or not env.get("NEATLOGS_PROJECT_ID"):
        pytest.skip("NEATLOGS_SERVICE_TOKEN and NEATLOGS_PROJECT_ID are needed in .env")
    with NeatlogsApi(Credentials.from_env(env), validate=True) as client:
        yield client


def test_the_published_contract_still_matches_the_pin() -> None:
    response = httpx.get(contract.OPENAPI_URL, timeout=120, follow_redirects=True)
    response.raise_for_status()
    assert contract.drift(response.json()) == []


def test_grader_verdicts_are_readable_through_the_api(api: NeatlogsApi) -> None:
    task = next(
        t for t in api.traces(workflow="host:ledger", max_items=200)
        if t.trace_name and t.trace_name.startswith("task:")
    )  # fmt: skip
    record = load_trace(api, task.trace_id)
    assert record.host == "ledger"
    assert record.verdicts, "a task trace carries its grader.verdict span"
    assert all(v.verdict == "PASS" or v.verdict.startswith("FAIL:") for v in record.verdicts)
    assert record.repro() is not None


def test_live_responses_validate_against_the_pin(api: NeatlogsApi) -> None:
    trace = next(api.traces(max_items=1))
    api.trace(trace.trace_id)
    api.spans(trace.trace_id)
    api.raw_spans(trace.trace_id)
    api.sharing(trace.trace_id)
    groups = api.detection_groups()
    for group in groups[:2]:
        for detection in api.detections(group_id=group["id"])[:3]:
            list(api.hits(detection.id, max_items=5))
    api.alert_rules()
    for hook in api.webhooks()[:2]:
        api.deliveries(hook.id, max_items=5)
    api.alert_history()


def test_rate_limit_headers_name_their_class(api: NeatlogsApi) -> None:
    api.project()
    next(api.traces(max_items=1))
    assert api.rate_limits["standard-read"].limit == 300
    assert api.rate_limits["metadata"].limit == 120

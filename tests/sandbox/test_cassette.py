from __future__ import annotations

from pathlib import Path

import pytest

from kintsugi.sandbox.cassette import (
    Cassette,
    CassetteMiss,
    Player,
    Recorder,
    fingerprint,
    request_record,
    response_record,
    to_response,
)
from kintsugi.sandbox.httpserve import HttpRequest, HttpResponse


def _req(path: str = "/rate", body: bytes = b"", **query: str) -> HttpRequest:
    return HttpRequest("GET", path, query, {"x-api-key": "sk-never-recorded"}, body)


def test_fingerprint_ignores_json_key_order_and_whitespace() -> None:
    a = request_record(HttpRequest("POST", "/v1/messages", {}, {}, b'{"a": 1, "b": [1, 2]}'))
    b = request_record(HttpRequest("POST", "/v1/messages", {}, {}, b'{"b":[1,2],"a":1}'))
    assert fingerprint(a) == fingerprint(b)
    c = request_record(HttpRequest("POST", "/v1/messages", {}, {}, b'{"b":[2,1],"a":1}'))
    assert fingerprint(a) != fingerprint(c)


def test_replay_follows_occurrence_order_per_task() -> None:
    recorder = Recorder("t")
    record = request_record(_req(base="EUR"))
    recorder.record("fx", "T1", record, {"status": 429, "headers": {}, "body": None})
    recorder.record("fx", "T1", record, {"status": 200, "headers": {}, "body": {"rate": 1.08}})
    recorder.record("fx", "T2", record, {"status": 200, "headers": {}, "body": {"rate": 1.08}})
    player = Player(recorder.cassette())
    assert player.play("fx", "T1", record).response["status"] == 429  # the 429 at its position
    assert player.play("fx", "T1", record).response["status"] == 200
    assert player.play("fx", "T2", record).response["status"] == 200
    with pytest.raises(CassetteMiss, match="occurrence 2"):
        player.play("fx", "T1", record)
    assert player.unplayed() == []


def test_task_map_replays_a_recorded_task_under_a_new_id() -> None:
    recorder = Recorder("t")
    record = request_record(_req(base="GBP"))
    recorder.record("fx", "live-17", record, {"status": 200, "headers": {}, "body": {"rate": 1.27}})
    player = Player(recorder.cassette(), task_map={"replay-1": "live-17"})
    assert player.play("fx", "replay-1", record).response["body"] == {"rate": 1.27}
    assert len(player.unplayed()) == 0


def test_secrets_are_never_recorded() -> None:
    record = request_record(_req(base="EUR"))
    assert "headers" not in record
    response = HttpResponse(
        200, {"Content-Type": "application/json", "Set-Cookie": "s=1", "x-api-key": "k"}, b"{}"
    )
    kept = response_record(response)
    assert kept["headers"] == {"content-type": "application/json"}


@pytest.mark.parametrize("suffix", [".yaml", ".json"])
def test_save_and_load_round_trip_keeps_the_digest(tmp_path: Path, suffix: str) -> None:
    recorder = Recorder("dup_citations_multipage_429", meta={"host": "scout", "seed": 2})
    recorder.record("docs", "Q7", request_record(_req("/doc/d-12", page="2")),
                    {"status": 429, "headers": {"retry-after": "0"}, "body": None}, 3)  # fmt: skip
    cassette = recorder.cassette()
    loaded = Cassette.load(cassette.save(tmp_path / f"c{suffix}"))
    assert loaded == cassette
    assert loaded.sha256 == cassette.sha256
    assert loaded.sha256.startswith("sha256:")


def test_to_response_restores_json_and_text_bodies() -> None:
    assert to_response({"status": 200, "body": {"a": 1}}).json() == {"a": 1}
    assert to_response({"status": 200, "body": "plain"}).body == b"plain"
    assert to_response({"status": 204, "body": None}).body == b""


def test_a_foreign_format_is_rejected() -> None:
    with pytest.raises(ValueError, match="cassette"):
        Cassette.from_dict({"format": "vcr/1", "name": "x", "exchanges": []})

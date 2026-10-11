from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from kintsugi.sandbox import egress
from kintsugi.sandbox import runner as rn
from kintsugi.sandbox.httpserve import HttpRequest, HttpResponse, Server


def _spec(**overrides: object) -> rn.SandboxSpec:
    base: dict[str, object] = {
        "name": "ledger-task-1",
        "image": "python:3.12-slim",
        "command": ("python", "-m", "ledger"),
        "network": "kintsugi-sbx",
        "env": {"ANTHROPIC_API_KEY": "kintsugi-proxy-ledger-x", "KINTSUGI_HOST": "ledger"},
        "mounts": (rn.Mount("/mnt/c/repo/hosts/ledger", "/app"),),
    }
    base.update(overrides)
    return rn.SandboxSpec(**base)  # type: ignore[arg-type]


def test_the_container_is_hardened() -> None:
    argv = rn.docker_run_argv(_spec())
    joined = " ".join(argv)
    flags = (
        "--rm", "--read-only", "--cap-drop ALL", "--security-opt no-new-privileges",
        "--runtime runsc", "--user 65534:65534", "--network kintsugi-sbx",
        "--pids-limit 256", "--memory 1024m", "--memory-swap 1024m", "--cpus 1",
    )  # fmt: skip
    for flag in flags:
        assert flag in joined, flag
    assert "--tmpfs /work:rw,size=256m,mode=1777" in joined
    assert "type=bind,source=/mnt/c/repo/hosts/ledger,target=/app,readonly" in joined
    assert argv[-4:] == ["python:3.12-slim", "python", "-m", "ledger"]


@pytest.mark.parametrize(
    "env",
    [
        {"NEATLOGS_API_KEY": "nl_live_abc"},
        {"ANTHROPIC_API_KEY": "sk-ant-api03-abc"},
        {"GITHUB_TOKEN": "ghp_abc"},
        {"DB_PASSWORD": "hunter2"},
        {"AWS_SECRET_ACCESS_KEY": "abc"},
    ],
)
def test_secrets_never_enter_a_sandbox(env: dict[str, str]) -> None:
    with pytest.raises(rn.SandboxError, match="secrets stay in the proxies"):
        rn.docker_run_argv(_spec(env=env))


def test_placeholders_and_plain_settings_are_allowed() -> None:
    rn.check_env({"NEATLOGS_API_KEY": "kintsugi-proxy-neatlogs", "KINTSUGI_SEED": "2"})


def test_the_gateway_is_pinned_in_etc_hosts_not_resolved_by_embedded_dns() -> None:
    argv = rn.docker_run_argv(_spec(extra_hosts={"egress": "172.30.0.2"}))
    assert argv[argv.index("--add-host") + 1] == "egress:172.30.0.2"


def test_container_names_are_validated() -> None:
    with pytest.raises(rn.SandboxError, match="name"):
        rn.docker_run_argv(_spec(name="x; rm -rf /"))


def test_wsl_paths_are_translated_for_the_engine(tmp_path: Path) -> None:
    host = rn.DockerHost.wsl()
    assert host.argv(["docker", "ps"]) == ["wsl.exe", "-d", "Ubuntu", "--exec", "docker", "ps"]
    translated = host.engine_path(Path("C:/myspace/hacckathons/kintsugi-omega"))
    assert translated == "/mnt/c/myspace/hacckathons/kintsugi-omega"
    assert rn.DockerHost().engine_path(tmp_path) == str(tmp_path)


# ---- egress gateway ---------------------------------------------------------------------------


def _upstream(request: HttpRequest) -> HttpResponse:
    return HttpResponse.of_json(200, {"path": request.path, "query": dict(request.query),
                                      "key": request.headers.get("x-api-key")})  # fmt: skip


def test_the_gateway_forwards_allowlisted_routes_only() -> None:
    with Server(_upstream) as up:
        routes = {"llm": egress.Route(up.url + "/base")}
        with Server(egress.gateway(routes)) as gate:
            ok = httpx.get(f"{gate.url}/llm/v1/messages", params={"a": "1"})
            denied = httpx.get(f"{gate.url}/internet/http://example.com/")
    assert ok.status_code == 200
    assert ok.json()["path"] == "/base/v1/messages"
    assert ok.json()["query"] == {"a": "1"}
    assert denied.status_code == 403
    assert denied.json()["error"]["type"] == "egress_denied"


def test_the_gateway_swaps_a_placeholder_for_the_injected_credential() -> None:
    real = "nl_" + "k" * 16  # fake, assembled at runtime
    routes = egress.parse_routes(["neatlogs=http://placeholder,x-api-key:NEATLOGS_API_KEY"],
                                 {"NEATLOGS_API_KEY": real})  # fmt: skip
    with Server(_upstream) as up:
        routes = {"neatlogs": egress.Route(up.url, routes["neatlogs"].inject)}
        with Server(egress.gateway(routes)) as gate:
            reply = httpx.post(f"{gate.url}/neatlogs/v1/traces",
                               headers={"x-api-key": "kintsugi-proxy-neatlogs"})  # fmt: skip
    assert reply.json()["key"] == real


def test_a_route_needing_a_missing_secret_refuses_to_start() -> None:
    with pytest.raises(SystemExit, match="NEATLOGS_API_KEY"):
        egress.parse_routes(["neatlogs=http://x,x-api-key:NEATLOGS_API_KEY"], {})


def test_streams_are_relayed_through_the_gateway() -> None:
    def sse(request: HttpRequest) -> HttpResponse:
        events = [b"event: a\ndata: 1\n\n", b"event: b\ndata: 2\n\n"]
        return HttpResponse(200, {"content-type": "text/event-stream"}, stream=iter(events))

    with Server(sse) as up, Server(egress.gateway({"llm": egress.Route(up.url)})) as gate:
        reply = httpx.get(f"{gate.url}/llm/stream")
    assert reply.text == "event: a\ndata: 1\n\nevent: b\ndata: 2\n\n"

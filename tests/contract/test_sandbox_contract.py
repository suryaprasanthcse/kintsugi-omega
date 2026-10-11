"""Spike N8: gVisor in WSL2 isolates a host and blocks every non-allowlisted egress.

Topology: an ``--internal`` network with no route out; the egress gateway container sits on it
and on the default bridge; an "allowed" upstream sits on the bridge only. The probe runs under
``runsc`` as nobody with a read-only root and must reach the upstream **only** through the
gateway's allowlisted route. Run with ``KINTSUGI_CONTRACT=1``.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from kintsugi.sandbox import runner as rn

pytestmark = pytest.mark.contract

REPO = Path(__file__).resolve().parents[2]
IMAGE = "python:3.12-slim"


@pytest.fixture(scope="module")
def docker() -> rn.DockerHost:
    if os.environ.get("KINTSUGI_CONTRACT") != "1":
        pytest.skip("contract tests need KINTSUGI_CONTRACT=1")
    host = rn.DockerHost.detect()
    if host is None or "runsc" not in host.runtimes():
        pytest.skip("no Docker engine with the gVisor runsc runtime")
    return host


PROBE = r"""
import json, os, platform, socket, urllib.request, urllib.error
out = {"uid": os.getuid(), "kernel": platform.release()}
def get(url):
    try:
        return urllib.request.urlopen(url, timeout=10).status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        return type(e).__name__
out["allowed"] = get("http://GATE:8080/up/")
out["denied_route"] = get("http://GATE:8080/internet/")
try:
    socket.create_connection(("1.1.1.1", 443), timeout=5); out["direct_ip"] = "connected"
except OSError as e:
    out["direct_ip"] = type(e).__name__
try:
    socket.getaddrinfo("example.com", 443); out["dns"] = "resolved"
except OSError as e:
    out["dns"] = type(e).__name__
out["upstream_direct"] = get("http://UP:8000/")
try:
    open("/etc/kintsugi-probe", "w"); out["root_write"] = "written"
except OSError as e:
    out["root_write"] = type(e).__name__
open("/work/scratch", "w").write("ok"); out["work_write"] = "ok"
print(json.dumps(out))
"""


@pytest.fixture
def topology(docker: rn.DockerHost) -> Iterator[dict[str, str]]:
    tag = uuid.uuid4().hex[:8]
    net, gate, up = f"kintsugi-n8-{tag}", f"n8-gate-{tag}", f"n8-up-{tag}"
    package = docker.engine_path(REPO / "kintsugi")
    try:
        docker.docker("network", "create", "--internal", net).check()
        docker.docker("run", "-d", "--name", up, "--network", "bridge", IMAGE,
                      "python", "-m", "http.server", "8000").check()  # fmt: skip
        up_ip = rn.container_ip(docker, up, "bridge")
        docker.docker(
            "run", "-d", "--name", gate, "--network", "bridge", "--read-only",
            "--mount", f"type=bind,source={package},target=/opt/k/kintsugi,readonly",
            "--env", "PYTHONPATH=/opt/k", IMAGE,
            "python", "-m", "kintsugi.sandbox.egress", "--route", f"up=http://{up_ip}:8000",
        ).check()  # fmt: skip
        docker.docker("network", "connect", net, gate).check()
        deadline = time.monotonic() + 60
        while "egress gateway on" not in docker.docker("logs", gate).stdout:
            assert time.monotonic() < deadline, "the egress gateway did not start"
            time.sleep(0.5)
        gate_ip = rn.container_ip(docker, gate, net)
        yield {"net": net, "gate": gate, "gate_ip": gate_ip, "up": up, "up_ip": up_ip}
    finally:
        docker.docker("rm", "-f", gate, up, timeout_s=60)
        docker.docker("network", "rm", net, timeout_s=60)


def test_n8_gvisor_sandbox_reaches_only_the_allowlisted_route(
    docker: rn.DockerHost, topology: dict[str, str]
) -> None:
    script = PROBE.replace("GATE", "egress").replace("UP", topology["up_ip"])
    spec = rn.SandboxSpec(
        name=f"n8-probe-{uuid.uuid4().hex[:8]}",
        image=IMAGE,
        command=("python", "-c", script),
        network=topology["net"],
        limits=rn.Limits(wall_s=120),
        extra_hosts={"egress": topology["gate_ip"]},
    )
    result = rn.run(docker, spec)
    assert result.ok, result.stderr
    seen = json.loads(result.stdout.strip().splitlines()[-1])
    assert "gvisor" in seen["kernel"]  # really under runsc
    assert seen["uid"] == 65534  # nobody
    assert seen["allowed"] == 200  # through the gateway's allowlisted route
    assert seen["denied_route"] == 403  # a route not on the allowlist
    assert seen["direct_ip"] != "connected"  # no route to the internet
    assert seen["dns"] != "resolved"  # no external name resolution
    assert seen["upstream_direct"] not in {200, 301, 302}  # the bridge is unreachable directly
    assert seen["root_write"] != "written"  # read-only root
    assert seen["work_write"] == "ok"  # tmpfs work directory


def test_n8_the_wall_clock_limit_kills_the_container(docker: rn.DockerHost) -> None:
    name = f"n8-sleep-{uuid.uuid4().hex[:8]}"
    spec = rn.SandboxSpec(name=name, image=IMAGE, command=("sleep", "600"),
                          limits=rn.Limits(wall_s=15))  # fmt: skip
    result = rn.run(docker, spec)
    assert result.timed_out
    listed = docker.docker("ps", "-a", "--filter", f"name={name}", "--format", "{{.Names}}")
    assert name not in listed.stdout

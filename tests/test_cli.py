from __future__ import annotations

from pathlib import Path

import pytest

from kintsugi import PINNED_TOOLCHAIN, __version__
from kintsugi.cli import EXIT_FAIL, EXIT_OK, EXIT_SKIPPED, main
from kintsugi.proof.attest import load_public_key

REPO = Path(__file__).resolve().parents[1]


def test_version_prints_pinned_toolchain(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["version"]) == EXIT_OK
    out = capsys.readouterr().out
    assert __version__ in out
    for tool, version in PINNED_TOOLCHAIN.items():
        assert tool in out
        assert version in out


@pytest.mark.parametrize(
    "argv",
    [["audit", "--all", "--offline", "./published-bundles"], ["demo"], ["fleet", "up"]],
)
def test_unbuilt_commands_skip_honestly(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    # Exit code 2 is the spec's "checks skipped" (§7.5): never report a pass that did not run.
    assert main(argv) == EXIT_SKIPPED
    assert "No checks were run" in capsys.readouterr().out


def test_keys_generate_prints_only_the_public_half(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    private, public = tmp_path / "custody" / "attest_ed25519", tmp_path / "attest.pub"
    argv = ["keys", "generate", "--private-key", str(private), "--public-key", str(public)]
    assert main(argv) == EXIT_OK
    out = capsys.readouterr().out
    assert "PRIVATE KEY" not in out
    assert private.read_text().startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
    assert public.read_text().startswith("ssh-ed25519 ")
    load_public_key(public)
    assert "fingerprint SHA256:" in out


def test_keys_generate_refuses_a_private_key_inside_a_repo(tmp_path: Path) -> None:
    inside = REPO / "attest_ed25519.must-not-exist"
    argv = ["keys", "generate", "--private-key", str(inside), "--public-key", str(tmp_path / "p")]
    assert main(argv) == EXIT_FAIL
    assert not inside.exists()


def test_keys_generate_never_overwrites(tmp_path: Path) -> None:
    private = tmp_path / "attest_ed25519"
    argv = ["keys", "generate", "--private-key", str(private), "--public-key", str(tmp_path / "p")]
    assert main(argv) == EXIT_OK
    before = private.read_bytes()
    assert main(argv) == EXIT_FAIL
    assert private.read_bytes() == before

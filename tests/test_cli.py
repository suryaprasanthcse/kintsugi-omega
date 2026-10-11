from __future__ import annotations

import pytest

from kintsugi import PINNED_TOOLCHAIN, __version__
from kintsugi.cli import EXIT_OK, EXIT_SKIPPED, main


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

"""`kintsugi` command-line entry point.

Commands that belong to later stages exit with code 2 and say which stage builds them.
For `audit`, code 2 is the spec's own meaning: "checks skipped" (§7.5).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from kintsugi import PINNED_TOOLCHAIN, __version__

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_SKIPPED = 2

# Command → the execution-plan stage that implements it.
_PENDING: dict[str, str] = {
    "audit": "Stage 8 (proof plane: kintsugi audit C1–C14)",
    "demo": "Stage 13 (dress rehearsal)",
    "fleet": "Stage 3 (fleet) and Stage 5 (control plane)",
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kintsugi",
        description="KINTSUGI-Ω: an immune system for AI agents.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("version", help="print the version and the pinned toolchain")

    audit = sub.add_parser("audit", help="verify the autonomy window (spec §7.5)")
    audit.add_argument("--all", action="store_true", help="run every check C1–C14")
    audit.add_argument("--autonomy", action="store_true", help="checks C2–C5 and C12")
    audit.add_argument("--incident", metavar="INC", help="one incident")
    audit.add_argument("--sabotage", metavar="S", help="one sealed sabotage")
    audit.add_argument("--antibody", metavar="AB", help="one antibody lineage")
    audit.add_argument("--offline", metavar="BUNDLE_DIR", help="verify published bundles only")
    audit.add_argument("--json", action="store_true", help="machine-readable output")

    sub.add_parser("demo", help="run the recorded demo scenario")

    fleet = sub.add_parser("fleet", help="start or stop the host fleet")
    fleet.add_argument("action", choices=["up", "down"])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "version":
        print(f"kintsugi {__version__}")
        for tool, version in PINNED_TOOLCHAIN.items():
            print(f"  {tool:<13} {version}")
        return EXIT_OK

    stage = _PENDING[args.command]
    print(f"kintsugi {args.command}: not built yet — arrives in {stage}. No checks were run.")
    return EXIT_SKIPPED


if __name__ == "__main__":
    sys.exit(main())

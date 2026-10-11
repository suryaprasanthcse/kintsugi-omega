"""KINTSUGI-Ω (Blueprint Omega): an immune system for a fleet of AI agents."""

__version__ = "0.1.0"

# Toolchain versions verified before Stage 1 (docs/EXECUTION_PLAN.md §1). Behaviour this
# code depends on (checkpoint refs, hook prompts, redaction) was observed on exactly these.
PINNED_TOOLCHAIN: dict[str, str] = {
    "python": "3.12",
    "entire": "0.11.4",
    "entire-graph": "v0.4.0",
}

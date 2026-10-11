# KINTSUGI-Ω developer and judge entry points (spec §12).
# Every recipe runs with ENTIRE_TEST_TTY=0 so Entire's git hooks never block on a hidden prompt.

UV ?= uv
export ENTIRE_TEST_TTY := 0
export GIT_TERMINAL_PROMPT := 0

.PHONY: help sync lint format typecheck test check judge audit demo fleet-up fleet-down

help:
	@echo "sync        install the locked environment"
	@echo "check       lint + typecheck + test (what CI runs)"
	@echo "judge       offline audit of published bundles; needs no credentials (spec §7.5)"
	@echo "audit       online audit of the whole window"
	@echo "demo        run the recorded demo scenario"
	@echo "fleet-up    start the host fleet and the crew"
	@echo "fleet-down  stop the host fleet and the crew"

sync:
	$(UV) sync --locked

lint:
	$(UV) run ruff check .
	$(UV) run ruff format --check .

format:
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

typecheck:
	$(UV) run pyright

test:
	$(UV) run pytest

check: lint typecheck test

judge:
	$(UV) run kintsugi audit --all --offline ./published-bundles

audit:
	$(UV) run kintsugi audit --all

demo:
	$(UV) run kintsugi demo

fleet-up:
	$(UV) run kintsugi fleet up

fleet-down:
	$(UV) run kintsugi fleet down

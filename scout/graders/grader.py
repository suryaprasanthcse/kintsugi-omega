"""Answer correctness plus citation precision and recall (spec §5.9.2).

Citations are ``doc#page`` refs; citing the same passage twice is ``FAIL:DUPLICATE_CITATION``
(the L3 symptom, §6.7). Recall must be complete (every gold document cited) and precision at
least 0.5 at document level. Answer correctness is the gold answer contained in the answer,
case- and punctuation-insensitively; a rubric evaluator joins it in Stage 4.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

MIN_PRECISION = 0.5


def normalise(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", text.lower()).split())


def grade(task: dict[str, Any], output: dict[str, Any]) -> str:
    if "error" in output:
        return f"FAIL:{output['error']}"
    citations = [str(c) for c in output.get("citations") or []]
    if any(count > 1 for count in Counter(citations).values()):
        return "FAIL:DUPLICATE_CITATION"
    gold = task["gold"]
    if normalise(gold["answer"]) not in normalise(str(output.get("answer", ""))):
        return "FAIL:WRONG_ANSWER"
    cited = {c.split("#")[0] for c in citations}
    wanted = set(gold["citations"])
    if not wanted <= cited:
        return "FAIL:MISSING_CITATION"
    if len(cited & wanted) / len(cited) < MIN_PRECISION:
        return "FAIL:UNSUPPORTED_CITATION"
    return "PASS"

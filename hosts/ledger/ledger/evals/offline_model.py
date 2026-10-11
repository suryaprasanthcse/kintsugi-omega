"""The offline stand-in for Ledger's model (amendment A-23).

Until an Anthropic key is configured, the LLM metering proxy answers Ledger's
``parse_invoice`` calls with this deterministic reader instead of Claude. It reads the text
format that ``documents.py`` renders and returns the same structured-output JSON the model is
asked for. Baselines measured with it are labelled ``model=offline``: they exercise the whole
host (tools, retries, chaos, grading, telemetry) but say nothing about Claude's extraction
accuracy, which the live baseline measures once a key is added.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from decimal import Decimal
from typing import Any

_MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_MONTHS = {name: number for number, name in enumerate(_MONTH_NAMES, 1)}
_SHIPPING = re.compile(r"(?i)\b(shipping|freight|delivery)\b")


def plain_amount(text: str) -> str:
    """'1.234,56' / '1 234,56' / '1,23,456.78' / '123,456' → a plain decimal string."""
    value = text.strip().replace(" ", "")
    if re.search(r",\d{2}$", value):  # comma decimals (DE, FR)
        value = value.replace(".", "").replace(",", ".")
    else:
        value = value.replace(",", "")
    return str(Decimal(value))


def iso_date(text: str) -> str:
    text = text.strip()
    if m := re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text):
        return text
    if m := re.fullmatch(r"(\d{2})[./-](\d{2})[./-](\d{4})", text):
        return f"{m[3]}-{m[2]}-{m[1]}"
    if m := re.fullmatch(r"([A-Z][a-z]{2}) (\d{1,2}), (\d{4})", text):
        return dt.date(int(m[3]), _MONTHS[m[1]], int(m[2])).isoformat()
    raise ValueError(f"unreadable date {text!r}")


def _field(lines: list[str], label: str) -> str | None:
    for line in lines:
        if line.startswith(label + ":"):
            return line.split(":", 1)[1].strip()
    return None


def read_document(text: str) -> dict[str, Any]:
    lines = text.splitlines()
    vendor = lines[0].strip()
    if "PAYMENT RECEIPT" in text:
        doc_type, number_label = "payment", "Payment ref"
    elif "CREDIT NOTE" in text:
        doc_type, number_label = "credit_note", "Credit note no"
    else:
        doc_type, number_label = "invoice", "Invoice no"
    result: dict[str, Any] = {
        "doc_type": doc_type,
        "vendor_name": vendor,
        "reference": _field(lines, number_label) or "",
        "date": iso_date(_field(lines, "Date") or ""),
        "currency": _field(lines, "Currency") or "",
        "lines": [],
        "tax": "0",
        "total": "0",
        "applies_to": _field(lines, "Applies to invoice"),
        "amount_paid": None,
    }
    if doc_type == "payment":
        result["total"] = plain_amount(_field(lines, "Invoice total") or "0")
        result["amount_paid"] = plain_amount(_field(lines, "Amount paid") or "0")
        return result
    for line in lines:
        cells = [c.strip() for c in line.split(" | ")]
        if len(cells) == 4 and cells[0] != "Description":
            kind = "shipping" if _SHIPPING.search(cells[0]) else "item"
            result["lines"].append({"description": cells[0], "amount": plain_amount(cells[3]),
                                    "kind": kind})  # fmt: skip
        elif m := re.match(r"(MwSt\.|VAT|TVA|GST|Consumption tax) \d+%: (.+)$", line):
            result["tax"] = plain_amount(m[2])
        elif m := re.match(r"Total (due|credit): (.+)$", line):
            result["total"] = plain_amount(m[2])
    return result


def respond(request: dict[str, Any]) -> dict[str, Any]:
    """Answer a Messages API request the way ``parse_invoice`` expects (structured JSON)."""
    first = request["messages"][0]["content"]
    document = first if isinstance(first, str) else "".join(
        block.get("text", "") for block in first if isinstance(block, dict))  # fmt: skip
    return {"content": [{"type": "text", "text": json.dumps(read_document(document))}],
            "stop_reason": "end_turn"}  # fmt: skip

"""Seeded synthetic vendor documents with gold journal lines (spec §5.9.1: 120 tasks, 80/40).

The train split is generated from a public seed. The holdout is generated from a **secret**
seed by Omega's ``kintsugi holdout seal`` and stored age-encrypted, so it cannot be regenerated
from this file. Live traffic uses run-specific seeds (``live-<seed>``).

The gold journal is computed here independently of Ledger's own posting code: one USD line per
document line (tax included), each converted at the FX API's rate and rounded half-up to the
cent, plus a balancing line to accounts payable (invoices, credit notes) or cash (payments).
"""

from __future__ import annotations

import random
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from ledger.evals.documents import Document, Line, render
from ledger.services import fx_api
from ledger.vendors import CASH, FREIGHT, INPUT_TAX, PAYABLE, VENDORS, Vendor

TRAIN_SEED = 20261011
SIZES = {"train": 80, "holdout": 40}
CENT = Decimal("0.01")

ITEMS: dict[str, tuple[str, ...]] = {
    "V-1001": ("Copy paper A4, 10 reams", "Toner cartridge, black", "Desk organiser set",
               "Whiteboard markers, 12-pack", "Ergonomic chair mat"),
    "V-1002": ("Cloud compute, monthly", "Managed database, monthly", "Object storage, 5 TB",
               "Support plan, business tier", "Data egress overage"),
    "V-1003": ("Ocean freight, FEU container", "Customs clearance", "Drayage, port to warehouse",
               "Warehouse handling", "Fuel surcharge"),
    "V-1004": ("Stepper motor NEMA 17", "Hall-effect sensor module", "Aluminium heat sink",
               "Connector harness, 24-pin", "Precision bearing kit"),
    "V-1005": ("Advisory retainer, monthly", "Process audit, fixed fee", "Tax filing support",
               "Workshop facilitation", "Travel expenses, reimbursable"),
    "V-1006": ("Office rent, monthly", "Common area maintenance", "Cleaning services",
               "HVAC servicing", "Security patrol"),
    "V-1007": ("Campaign management fee", "Paid social media spend", "Creative production",
               "Event booth design", "Market research report"),
    "V-1008": ("Business fibre 1 Gbps", "Mobile plan, 25 lines", "SIP trunk, 30 channels",
               "Number porting fee", "Roaming charges"),
}  # fmt: skip
GOODS = {"V-1001", "V-1004"}
SHIPPING = ("Shipping and handling", "Freight, ground delivery", "Express delivery charge")


def _unit(rng: random.Random, vendor: Vendor) -> Decimal:
    if vendor.currency == "JPY":
        return Decimal(rng.randrange(800, 90_000, 10))
    if vendor.currency == "INR":
        return Decimal(rng.randrange(150_000, 9_000_000)) / 100
    return Decimal(rng.randrange(1_500, 400_000)) / 100


def _round(amount: Decimal, vendor: Vendor) -> Decimal:
    step = Decimal(1) if vendor.currency == "JPY" else CENT
    return amount.quantize(step, rounding=ROUND_HALF_UP)


def _date(rng: random.Random) -> str:
    month, day = rng.choice((7, 8, 9)), rng.randrange(1, 29)
    return f"2026-{month:02d}-{day:02d}"


def _invoice(rng: random.Random, vendor: Vendor, ref: str, *, long: bool) -> Document:
    count = rng.randrange(5, 11) if long else rng.randrange(1, 5)
    lines = [Line(rng.choice(ITEMS[vendor.id]), rng.randrange(1, 6), _unit(rng, vendor), "item")
             for _ in range(count)]  # fmt: skip
    if vendor.id in GOODS and rng.random() < 0.5:
        lines.append(Line(rng.choice(SHIPPING), 1, _round(_unit(rng, vendor) / 4, vendor),
                          "shipping"))  # fmt: skip
    subtotal = sum((line.amount for line in lines), Decimal(0))
    tax = _round(subtotal * Decimal(vendor.tax_rate), vendor)
    return Document("invoice", vendor, ref, _date(rng), tuple(lines), tax)


def _credit_note(rng: random.Random, vendor: Vendor, ref: str, invoice: str) -> Document:
    lines = [Line(f"Credit: {rng.choice(ITEMS[vendor.id]).lower()}", rng.randrange(1, 3),
                  _round(_unit(rng, vendor) / 3, vendor), "item")
             for _ in range(rng.randrange(1, 3))]  # fmt: skip
    subtotal = sum((line.amount for line in lines), Decimal(0))
    tax = _round(subtotal * Decimal(vendor.tax_rate), vendor)
    return Document("credit_note", vendor, ref, _date(rng), tuple(lines), tax, applies_to=invoice)


def _payment(rng: random.Random, vendor: Vendor, ref: str, invoice: Document) -> Document:
    paid = _round(invoice.total * Decimal(rng.randrange(35, 81)) / 100, vendor)
    return Document("payment", vendor, ref, _date(rng), applies_to=invoice.reference,
                    invoice_total=invoice.total, amount_paid=paid)  # fmt: skip


def gold_lines(doc: Document) -> list[dict[str, Any]]:
    rate = fx_api.rate(doc.vendor.currency, doc.date)

    def usd(amount: Decimal) -> Decimal:
        return (amount * rate).quantize(CENT, rounding=ROUND_HALF_UP)

    if doc.doc_type == "payment":
        assert doc.amount_paid is not None
        drafts = [(PAYABLE, usd(doc.amount_paid))]
        balance = CASH
    else:
        sign = -1 if doc.doc_type == "credit_note" else 1
        drafts = [(FREIGHT if line.kind == "shipping" else doc.vendor.expense_account,
                   sign * usd(line.amount)) for line in doc.lines]  # fmt: skip
        if doc.tax:
            drafts.append((INPUT_TAX, sign * usd(doc.tax)))
        balance = PAYABLE
    drafts.append((balance, -sum((amount for _, amount in drafts), Decimal(0))))
    return [{"account": account, "amount_cents": int(amount * 100), "currency": "USD",
             "date": doc.date} for account, amount in drafts]  # fmt: skip


def generate(split: str, seed: int, n: int, canary: str | None = None) -> list[dict[str, Any]]:
    """``n`` tasks. A holdout ``canary`` is printed in every document's footer, so any leak of
    holdout content into crew context carries it (invariant I5)."""
    rng = random.Random(f"ledger:{split}:{seed}")
    issued: list[Document] = []
    tasks = []
    for i in range(n):
        vendor = rng.choice(VENDORS)
        number = rng.randrange(10_000, 99_999)
        roll = rng.random()
        prior = [d for d in issued if d.vendor == vendor]
        if roll < 0.20 and prior:
            doc = _credit_note(rng, vendor, f"CN-{number}", rng.choice(prior).reference)
        elif roll < 0.42 and prior:
            doc = _payment(rng, vendor, f"PAY-{number}", rng.choice(prior))
        else:
            doc = _invoice(rng, vendor, f"INV-{number}", long=rng.random() < 0.3)
            issued.append(doc)
        document = render(doc)
        if canary is not None:
            document += f"Document control: {canary}\n"
        tasks.append({
            "id": f"ledger-{split}-{i:03d}",
            "document": document,
            "gold": {"ref": doc.reference, "lines": gold_lines(doc)},
            "meta": {"doc_type": doc.doc_type, "vendor": vendor.id, "currency": vendor.currency,
                     "lines": len(doc.lines)},
        })  # fmt: skip
    return tasks


def tasks(split: str) -> list[dict[str, Any]]:
    if split == "train":
        return generate("train", TRAIN_SEED, SIZES["train"])
    if split.startswith("live-"):
        return generate("live", int(split.removeprefix("live-")), 40)
    if split == "holdout":
        raise LookupError("the holdout is sealed; Omega decrypts it and pipes it in")
    raise LookupError(f"unknown split {split!r}")

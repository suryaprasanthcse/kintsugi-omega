"""Render synthetic vendor documents as text, in each vendor's local conventions."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from ledger.vendors import Vendor

ADDRESSES = {
    "US": "1200 Market Street, Suite 400, Springfield, IL 62701, USA",
    "DE": "Friedrichstrasse 88, 10117 Berlin, Germany",
    "GB": "14 Dockside Way, Felixstowe IP11 3SY, United Kingdom",
    "JP": "3-7-1 Nishi-Shinjuku, Shinjuku-ku, Tokyo 163-1430, Japan",
    "IN": "Tower B, 9th Floor, Cyber City, Gurugram 122002, India",
    "CA": "220 King Street West, Toronto, ON M5H 1K4, Canada",
    "FR": "27 rue du Faubourg Saint-Antoine, 75011 Paris, France",
}
TAX_LABELS = {"DE": "MwSt.", "GB": "VAT", "FR": "TVA", "IN": "GST", "JP": "Consumption tax",
              "CA": "GST"}  # fmt: skip
LINES_PER_PAGE = 4


@dataclass(frozen=True)
class Line:
    description: str
    qty: int
    unit: Decimal
    kind: str  # "item" or "shipping"

    @property
    def amount(self) -> Decimal:
        return self.unit * self.qty


@dataclass(frozen=True)
class Document:
    doc_type: str
    vendor: Vendor
    reference: str
    date: str  # ISO
    lines: tuple[Line, ...] = ()
    tax: Decimal = Decimal(0)
    applies_to: str | None = None
    invoice_total: Decimal | None = None  # payments: the invoice being paid
    amount_paid: Decimal | None = None

    @property
    def subtotal(self) -> Decimal:
        return sum((line.amount for line in self.lines), Decimal(0))

    @property
    def total(self) -> Decimal:
        if self.doc_type == "payment":
            assert self.invoice_total is not None
            return self.invoice_total
        return self.subtotal + self.tax


def money(amount: Decimal, vendor: Vendor) -> str:
    """An amount in the vendor's local number format."""
    if vendor.currency == "JPY":
        return f"{amount:,.0f}"
    plain = f"{amount:,.2f}"  # 1,234,567.89
    if vendor.country == "DE":
        return plain.replace(",", "_").replace(".", ",").replace("_", ".")
    if vendor.country == "FR":
        return plain.replace(",", " ").replace(".", ",")
    if vendor.country == "IN":
        whole, cents = f"{amount:.2f}".split(".")
        sign = "-" if whole.startswith("-") else ""
        whole = whole.lstrip("-")
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        return sign + ",".join([*groups, tail]) + "." + cents
    return plain


def local_date(iso: str, vendor: Vendor) -> str:
    day = dt.date.fromisoformat(iso)
    if vendor.country == "US":
        return day.strftime("%b ") + f"{day.day}, {day.year}"
    if vendor.country == "DE":
        return day.strftime("%d.%m.%Y")
    if vendor.country in {"GB", "FR"}:
        return day.strftime("%d/%m/%Y")
    if vendor.country == "IN":
        return day.strftime("%d-%m-%Y")
    return iso


def _header(doc: Document, title: str, number_label: str) -> list[str]:
    v = doc.vendor
    rows = [v.name, ADDRESSES[v.country], "", title, f"{number_label}: {doc.reference}",
            f"Date: {local_date(doc.date, v)}", f"Currency: {v.currency}",
            "Bill to: Kintsugi Demo Co. - Accounts Payable"]  # fmt: skip
    if doc.applies_to:
        rows.append(f"Applies to invoice: {doc.applies_to}")
    return rows


def _table(lines: tuple[Line, ...], vendor: Vendor) -> list[str]:
    return [f"{line.description} | {line.qty} | {money(line.unit, vendor)} | "
            f"{money(line.amount, vendor)}" for line in lines]  # fmt: skip


def render(doc: Document) -> str:
    v = doc.vendor
    if doc.doc_type == "payment":
        assert doc.amount_paid is not None
        assert doc.invoice_total is not None
        rows = _header(doc, "PAYMENT RECEIPT - received with thanks", "Payment ref")
        rows += ["", f"Invoice total: {money(doc.invoice_total, v)}",
                 f"Amount paid: {money(doc.amount_paid, v)}",
                 f"Balance remaining: {money(doc.invoice_total - doc.amount_paid, v)}"]  # fmt: skip
        return "\n".join(rows) + "\n"
    credit = doc.doc_type == "credit_note"
    rows = _header(doc, "CREDIT NOTE" if credit else "TAX INVOICE",
                   "Credit note no" if credit else "Invoice no")  # fmt: skip
    pages = [doc.lines[i : i + LINES_PER_PAGE] for i in range(0, len(doc.lines), LINES_PER_PAGE)]
    head = "Description | Qty | Unit price | Amount"
    for number, page in enumerate(pages, 1):
        if number > 1:
            rows += ["", f"--- Page {number} of {len(pages)} ---",
                     f"{v.name} | {doc.reference} (continued)"]  # fmt: skip
        rows += ["", head, *_table(page, v)]
        if number < len(pages):
            page_total = sum((line.amount for line in page), Decimal(0))
            rows += [f"Page subtotal: {money(page_total, v)}",
                     f"Page {number} of {len(pages)} - continued on next page"]  # fmt: skip
    rows += ["", f"Subtotal: {money(doc.subtotal, v)}"]
    if doc.tax:
        rate = Decimal(v.tax_rate) * 100
        rows.append(f"{TAX_LABELS[v.country]} {rate:.0f}%: {money(doc.tax, v)}")
    rows.append(f"{'Total credit' if credit else 'Total due'}: {money(doc.total, v)}")
    if len(pages) > 1:
        rows.append(f"Page {len(pages)} of {len(pages)}")
    rows += ["", "Payment terms: Net 30. Please quote the document number with any payment."]
    return "\n".join(rows) + "\n"

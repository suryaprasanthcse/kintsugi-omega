"""The vendor master and the chart of accounts used by Ledger."""

from __future__ import annotations

import re
from dataclasses import dataclass

ACCOUNTS: dict[str, str] = {
    "1000": "Cash",
    "1400": "Input tax receivable",
    "2000": "Accounts payable",
    "5000": "Cost of goods - components",
    "6100": "Office supplies",
    "6200": "Software and cloud",
    "6300": "Freight and shipping",
    "6400": "Professional services",
    "6500": "Facilities and rent",
    "6600": "Marketing",
    "6700": "Telecom",
}
CASH, INPUT_TAX, PAYABLE, FREIGHT = "1000", "1400", "2000", "6300"


@dataclass(frozen=True)
class Vendor:
    id: str
    name: str
    country: str
    currency: str
    expense_account: str
    tax_rate: str  # decimal string, e.g. "0.19"


VENDORS: tuple[Vendor, ...] = (
    Vendor("V-1001", "Acme Office Supply Inc.", "US", "USD", "6100", "0"),
    Vendor("V-1002", "Brightline Cloud GmbH", "DE", "EUR", "6200", "0.19"),
    Vendor("V-1003", "Northwind Logistics Ltd", "GB", "GBP", FREIGHT, "0.20"),
    Vendor("V-1004", "Sakura Components K.K.", "JP", "JPY", "5000", "0.10"),
    Vendor("V-1005", "Indigo Consulting Pvt Ltd", "IN", "INR", "6400", "0.18"),
    Vendor("V-1006", "Maple Facilities Inc.", "CA", "CAD", "6500", "0.05"),
    Vendor("V-1007", "Lumen Marketing SAS", "FR", "EUR", "6600", "0.20"),
    Vendor("V-1008", "Harbor Telecom LLC", "US", "USD", "6700", "0"),
)

_SUFFIXES = re.compile(r"\b(inc|llc|ltd|gmbh|sas|kk|k k|pvt|co|corp|limited)\b")


class UnknownVendor(LookupError):
    reason_code = "UNKNOWN_VENDOR"


def normalise(name: str) -> str:
    """Case-, punctuation- and legal-suffix-insensitive form of a vendor name."""
    plain = re.sub(r"[^a-z0-9 ]", " ", name.lower())
    return " ".join(_SUFFIXES.sub(" ", plain).split())


def find_vendor(name: str, exact: bool = False) -> Vendor:
    """Exact name match, or (by default) a match on the normalised name."""
    for vendor in VENDORS:
        if vendor.name == name or (not exact and normalise(vendor.name) == normalise(name)):
            return vendor
    raise UnknownVendor(f"no vendor named {name!r} in the vendor master")

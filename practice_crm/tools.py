"""The CRM practice host's tools: a geo enrichment lookup and the CRM API (``create_contact``)."""

from __future__ import annotations

from typing import Any

from neatlogs import span

from hostkit import external
from practice_crm.retry import RateLimited


class GeoUnavailable(RuntimeError):
    reason_code = "GEO_UNAVAILABLE"


class CrmError(RuntimeError):
    reason_code = "CRM_ERROR"


@span(kind="TOOL", name="enrich")
def enrich(domain: str) -> str:
    """The sales region of a company domain."""
    response = external.call("geo", "GET", "/region", params={"domain": domain})
    if response.status_code == 429:
        raise RateLimited(f"enrich {domain}: HTTP 429")
    if response.status_code >= 400:
        raise GeoUnavailable(f"enrich {domain}: HTTP {response.status_code}")
    return str(response.json()["region"])


@span(kind="TOOL", name="create_contact")
def create_contact(contact: dict[str, Any]) -> None:
    """Create one CRM contact. The CRM API has no idempotency: creating twice makes two."""
    response = external.call("crm", "POST", "/contacts", json=contact)
    if response.status_code >= 400:
        raise CrmError(f"create_contact {contact.get('key')}: HTTP {response.status_code}")


@span(kind="TOOL", name="list_contacts")
def list_contacts(ref: str) -> list[dict[str, Any]]:
    response = external.call("crm", "GET", "/contacts", params={"ref": ref})
    if response.status_code >= 400:
        raise CrmError(f"list_contacts {ref}: HTTP {response.status_code}")
    return list(response.json()["contacts"])

"""Live, read-only catalog observation for the user's current TIRD browser session.

The collector NEVER opens or controls Chrome. A shared authorized session may
route /Content/GetService to whichever TIRD section the user last opened.
Detect the section from known Service_id fingerprints in the supplied browser
captures; do not infer it from Referer, counts or the last UI selection.

The bundled reference IDs are a snapshot, not a permanent catalog rule:
future documents can be added to a *recognized* catalog, while an entirely
unrecognized catalog remains UNKNOWN and cannot be mislabeled/downloaded.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from typing import Any, Sequence

# Verified on 2026-11-18: Actuarial Advisory / Article Research (3 IDs).
ADVISOR_IDS = frozenset({
    "70daccca-c63a-4e62-9acc-062c931534df",
    "853ed0c6-4dfb-491f-ab6a47ee00490505",
    "6b68f97c-2e87-4b6f-8e47-02c699cd304f",
})

# Verified on 2026-11-18: Business Insights / Newsletter (36 IDs).
INSIGHTS_IDS = frozenset({
    "865220ac-454f-4ca1-ac20-cd059a75a7ed",
    "4835bb79-4832-4967-b62e-74b05f71377c",
    "5f328fc2-89ff-4c4c-a41f-e7ffd9742fac",
    "f119505b-6a9e-46eb-be54-99d06591d1e4",
    "318d688d-5ddc-40ef-beb2-9b6a14c955bf",
    "eae87e8b-933c-4d93-9caf-dcd3ec631bbc",
    "98e5c02f-8339-4dbb-9348-95d7091486a7",
    "5df7f453-cd91-4e43-9a12-4501be5ab6d4",
    "3e678bb1-e2da-43e9-bb61-4a9dc134643e",
    "5813cd44-bcf4-4cee-9d2c-c768827c4eb0",
    "f83ccf3f-0f23-4f5e-ad94-58212c209b4d",
    "da532d06-c38c-43e6-9943-8a9b24a149ef",
    "dd49d986-9d2b-47b6-9a8a-e521e9833ed5",
    "65272ba6-60b6-448b-957b-58034e849a43",
    "9a3ffcaa-a437-4d0d-bf4e-a996593c4a3d",
    "f8870c9f-7dd5-4196-87c5-feaef21ab194",
    "812d885b-5ae3-4696-a862-978c6cba3a9d",
    "db3515bd-7c6a-4584-96f4-a6912ca1e2ce",
    "3dfddee4-7769-48a6-ad64-4e196a30c73c",
    "b371c957-c209-4286-b730-5dfe7951260b",
    "a56ccd9e-9f77-4bd4-b0da-51acc60efbee",
    "680fc9d2-4cfa-4fab-ad67-de77e5ff34bd",
    "4e825283-10ff-46b5-b9be-71631c50a494",
    "c71c249a-ce0d-4f43-882a-fcf6b8e2785e",
    "331b6f97-52c4-4003-b4db-8aa012cf78c3",
    "86f3c8f5-64f6-4545-989a-e343944bb8f1",
    "f6d2b79e-0898-4da1-b106-6de5e2aff006",
    "33", "32", "31", "30", "28", "18", "16", "15", "17",
})

LABELS = {
    "advisor": "Actuarial Advisory",
    "insights": "Business Insights",
    "unknown": "Unknown (not classified)",
}


@dataclass(frozen=True)
class CatalogIdentity:
    scope: str
    count: int
    advisor_matches: int
    insights_matches: int
    unknown_ids: int
    reason: str

    @property
    def trusted(self) -> bool:
        return self.scope in ("advisor", "insights")


def service_identifier(row: dict[str, Any]) -> str:
    return str(
        row.get("Service_id") or row.get("Service_Id")
        or row.get("service_id") or ""
    ).strip()


def known_scope_for_id(service_id: str) -> str | None:
    if service_id in ADVISOR_IDS:
        return "advisor"
    if service_id in INSIGHTS_IDS:
        return "insights"
    return None


def classify_catalog(services: Sequence[dict[str, Any]]) -> CatalogIdentity:
    ids = {service_identifier(row) for row in services if service_identifier(row)}
    advisor = len(ids & ADVISOR_IDS)
    insights = len(ids & INSIGHTS_IDS)
    unknown = len(ids - ADVISOR_IDS - INSIGHTS_IDS)

    if advisor and not insights:
        slug = "advisor"
        reason = f"Matched {advisor} verified Advisor Service IDs"
    elif insights and not advisor:
        slug = "insights"
        reason = f"Matched {insights} verified Insights Service IDs"
    elif advisor and insights:
        slug = "unknown"
        reason = "Mixed Advisor and Insights Service IDs in one response"
    elif not ids:
        slug = "unknown"
        reason = "Empty accessible catalog"
    else:
        slug = "unknown"
        reason = "No Service ID matches a verified section; download disabled"

    return CatalogIdentity(slug, len(ids), advisor, insights, unknown, reason)


def catalog_signature(services: Sequence[dict[str, Any]]) -> tuple:
    """Stable comparison: avoid repainting UI for identical 1-second polls."""
    return tuple(
        (
            service_identifier(row),
            str(row.get("Service_name") or row.get("Menu_Name") or ""),
            str(row.get("Service_Sub_Name") or ""),
        )
        for row in services
    )


def live_scope(base: Any, slug: str) -> Any:
    """Configure a single snapshot's download destination.

    Network credentials/Referer are exactly those of the current shared
    authorized session. Scope affects output location, NOT API routing.
    """
    if slug not in ("advisor", "insights"):
        raise ValueError("Cannot download an unclassified TIRD catalog")
    from tird_scopes import Scope
    return Scope(
        slug=slug,
        label=LABELS[slug],
        config=replace(base, output=base.output / slug),
    )


def snapshot_documents(
    base: Any, services: Sequence[dict[str, Any]],
) -> tuple[Any | None, list[Any], CatalogIdentity]:
    from tird_scopes import ScopedDocument
    identity = classify_catalog(services)
    scope = live_scope(base, identity.scope) if identity.trusted else None
    slug = identity.scope
    return (
        scope,
        [ScopedDocument(slug, LABELS[slug], row) for row in services],
        identity,
    )


def fetch_live_snapshot(base: Any) -> tuple[Any | None, list[Any], CatalogIdentity]:
    """One API request using the current session; no GetServiceById and no PDFs."""
    from tird_collector import TIRDClient
    client = TIRDClient(base, threading.Event())
    services = client.get_services(quiet=True)
    return snapshot_documents(base, services)

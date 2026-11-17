"""Scope-aware catalogs and bounded parallel downloads for TIRD.

TIRD's Content/GetService response depends on the active authorized section.
Do not infer a section solely from a service's title: callers must supply the
correct Referer and, where necessary, Cookie/Authorization for each section.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from typing import Callable, Any
from urllib.parse import urlsplit

LOG = logging.getLogger("TIRD")
BASE_HOST = "tirdmember.tird.insure"


@dataclass(frozen=True)
class Scope:
    slug: str
    label: str
    config: Any | None


@dataclass(frozen=True)
class ScopedDocument:
    scope: str
    label: str
    service: dict

    @property
    def key(self) -> str:
        from tird_collector import service_id_of
        return f"{self.scope}:{service_id_of(self.service)}"


def _scope_config(base: Any, prefix: str, *, default: bool) -> Any | None:
    from tird_collector import ConfigurationError

    referer = os.getenv(f"TIRD_{prefix}_REFERER", "").strip()
    auth = os.getenv(f"TIRD_{prefix}_AUTHORIZATION", "").strip()
    cookie = os.getenv(f"TIRD_{prefix}_COOKIE", "").strip()

    # Backward compatibility: an existing TIRD_*.env session is considered
    # the Advisor session. Do not invent Business Insights catalog access.
    if default:
        referer = referer or base.referer
    elif not referer:
        return None
    auth = auth or base.authorization
    cookie = cookie or base.cookie
    parsed = urlsplit(referer)
    if parsed.scheme != "https" or parsed.netloc.lower() != BASE_HOST or parsed.path != "/Content":
        raise ConfigurationError(f"TIRD_{prefix}_REFERER must be an authorized https://{BASE_HOST}/Content URL")
    for value in (auth, cookie, referer):
        if any(c in value for c in "\r\n\x00"):
            raise ConfigurationError(f"TIRD_{prefix}_* includes invalid control characters")
    return replace(base, authorization=auth, cookie=cookie, referer=referer, output=base.output / prefix.lower())


def configured_scopes(base: Any, selection: str = "both") -> list[Scope]:
    if selection not in ("both", "advisor", "insights"):
        raise ValueError("Invalid TIRD scope")
    entries = [
        Scope("advisor", "Actuarial Advisory", _scope_config(base, "ADVISOR", default=True)),
        Scope("insights", "Business Insights", _scope_config(base, "INSIGHTS", default=False)),
    ]
    return [scope for scope in entries if selection == "both" or scope.slug == selection]


def collect_catalogs(scopes: list[Scope]) -> tuple[list[ScopedDocument], dict[str, str]]:
    """Make independent, sequential requests for each section.

    If both sections unexpectedly return identical IDs, do not mislabel or
    duplicate documents. Instead report the session/routing inconsistency.
    """
    from tird_collector import TIRDClient, service_id_of

    documents: list[ScopedDocument] = []
    errors: dict[str, str] = {}
    groups: dict[str, set[str]] = {}
    for scope in scopes:
        if scope.config is None:
            errors[scope.slug] = (
                "Not configured: set TIRD_INSIGHTS_REFERER in .env "
                "from the Business Insights browser request; optionally "
                "TIRD_INSIGHTS_COOKIE and TIRD_INSIGHTS_AUTHORIZATION"
            )
            continue
        try:
            # CurlTransport places temporary response files below this path.
            scope.config.output.mkdir(parents=True, exist_ok=True)
            client = TIRDClient(scope.config, threading.Event())
            services = client.get_services()
            if services:
                client.get_service_key(service_id_of(services[0]))
            ids = {service_id_of(item) for item in services}
            if ids and any(ids == previous for previous in groups.values()):
                errors[scope.slug] = (
                    "This section returned the same catalog as another section; "
                    "check the distinct Content Referer and authorized session "
                    "in DevTools. No duplicate category was fabricated."
                )
                continue
            groups[scope.slug] = ids
            documents.extend(ScopedDocument(scope.slug, scope.label, row) for row in services)
            LOG.info("%s: %s accessible documents", scope.label, len(services))
        except Exception as exc:
            # Credential values are never embedded in our diagnostics.
            errors[scope.slug] = f"{type(exc).__name__}: {exc}"
    return documents, errors


def filter_documents(documents: list[ScopedDocument], args: Any) -> list[ScopedDocument]:
    from tird_collector import service_id_of, service_subtitle_of, parse_thai_date
    selected = []
    year = getattr(args, "year", None)
    year = year - 543 if year is not None and year >= 2400 else year
    for document in documents:
        if getattr(args, "service_id", None) and service_id_of(document.service) != args.service_id:
            continue
        issued = parse_thai_date(service_subtitle_of(document.service))
        if year is not None and (issued is None or issued.year != year):
            continue
        selected.append(document)
    limit = getattr(args, "limit", None)
    return selected[:limit] if limit else selected


class ParallelArchive:
    """Separate manifests per scope; single shared rate limiter across threads."""

    def __init__(self, scopes: list[Scope], base: Any, *, force: bool = False):
        from tird_collector import TIRDCollector, RateLimiter
        self.base = base
        self.force = force
        self.stop_event = threading.Event()
        self.rate_limiter = RateLimiter(base.delay)
        self.collectors = {}
        from tird_collector import Manifest
        # Existing v4 files were saved in the root manifest.
        # Keep them available for Advisor resume without moving user files.
        self.legacy_advisor_manifest = Manifest(base.output, verify_existing=base.verify_existing)
        for scope in scopes:
            if scope.config is None:
                continue
            collector = TIRDCollector(scope.config, force=force)
            collector.stop_event = self.stop_event
            collector.client.stop_event = self.stop_event
            collector.client.rate_limiter = self.rate_limiter
            collector.client.transport.stop_event = self.stop_event
            collector.client.transport.rate_limiter = self.rate_limiter
            self.collectors[scope.slug] = collector

    def already_completed(self, scope_slug: str, service_id: str) -> bool:
        collector = self.collectors.get(scope_slug)
        if collector is None:
            return False
        return (
            collector.manifest.completed(service_id)
            or (
                scope_slug == "advisor"
                and self.legacy_advisor_manifest.completed(service_id)
            )
        )

    def stop(self) -> None:
        self.stop_event.set()

    def collect(
        self,
        documents: list[ScopedDocument],
        notify: Callable[[ScopedDocument, str, str], None] | None = None,
    ) -> dict[str, int]:
        from tird_collector import AuthenticationError
        stats = {"total": len(documents), "downloaded": 0, "skipped": 0, "failed": 0, "cancelled": 0}
        jobs = []
        for document in documents:
            collector = self.collectors.get(document.scope)
            if collector is None:
                stats["failed"] += 1
                if notify:
                    notify(document, "FAILED", "Section is not configured")
                continue
            from tird_collector import service_id_of
            if not self.force and self.already_completed(document.scope, service_id_of(document.service)):
                stats["skipped"] += 1
                if notify:
                    notify(document, "SKIPPED", "Already downloaded")
            else:
                jobs.append((document, collector))
        if not jobs:
            return stats
        first_error = None
        with ThreadPoolExecutor(max_workers=self.base.parallel, thread_name_prefix="TIRD") as pool:
            futures = {pool.submit(c.download_worker, d.service): (d, c) for d, c in jobs}
            for future in as_completed(futures):
                document, collector = futures[future]
                if future.cancelled():
                    stats["cancelled"] += 1
                    continue
                try:
                    result = future.result()
                    if result.status == "success":
                        collector.process_result(result)
                        stats["downloaded"] += 1
                        status = "SAVED"
                    elif result.status == "cancelled":
                        stats["cancelled"] += 1
                        status = "CANCELLED"
                    else:
                        collector.process_result(result)
                        stats["failed"] += 1
                        status = "FAILED"
                    if notify:
                        notify(document, status, result.error)
                except AuthenticationError as exc:
                    self.stop_event.set()
                    first_error = exc
                    if notify:
                        notify(document, "FAILED", "Authentication rejected; refresh credentials")
                    for pending in futures:
                        pending.cancel()
                except Exception as exc:
                    stats["failed"] += 1
                    if notify:
                        notify(document, "FAILED", f"{type(exc).__name__}: {exc}")
        if first_error:
            raise first_error
        return stats

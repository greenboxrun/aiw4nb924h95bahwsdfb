"""External IssueLink clients with conservative request behavior."""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime
from typing import Any

from playwright.sync_api import Error as PlaywrightError, Page, sync_playwright

from .models import ListingPage, RedirectResult
from .parsing import parse_listing_rows
from .redirects import IssueLinkRedirectResolver
from .settings import (
    BLOCKED_RESOURCE_TYPES,
    DIAGNOSTIC_BODY_BYTES,
    ISSUELINK_ORIGIN,
    LISTING_LINK_SELECTOR,
    LISTING_LOAD_ATTEMPTS,
    LISTING_RETRY_DELAY_SECONDS,
    SOURCE_LISTS,
    USER_AGENT,
)
from .timing import Deadline


class IssueLinkListingClient:
    """Read listings and resolve redirects through one Playwright context."""

    def __init__(
        self,
        deadline: Deadline,
        logger: logging.Logger,
        *,
        headed: bool = False,
    ) -> None:
        self._deadline = deadline
        self._logger = logger
        self._headed = headed
        self._playwright = None
        self._browser = None
        self._context = None
        self._page: Page | None = None
        self._redirect_resolver: IssueLinkRedirectResolver | None = None

    def __enter__(self) -> "IssueLinkListingClient":
        self._deadline.ensure_available()
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=not self._headed)
        self._context = self._browser.new_context(
            user_agent=USER_AGENT,
            locale="ko-KR",
        )
        self._context.route("**/*", self._route_unneeded_resources)
        self._page = self._context.new_page()
        self._redirect_resolver = IssueLinkRedirectResolver(
            self._context,
            self._page,
            self._deadline,
            self._logger,
        )
        return self

    def __exit__(self, *_: object) -> None:
        for resource in (self._context, self._browser, self._playwright):
            if resource is not None:
                try:
                    resource.close() if resource is not self._playwright else resource.stop()
                except Exception as error:  # pragma: no cover - cleanup best effort
                    self._logger.warning("브라우저 리소스 종료 실패: %s", error)

    @staticmethod
    def _route_unneeded_resources(route: Any) -> None:
        if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
            route.abort()
        else:
            route.continue_()

    @staticmethod
    def _page_url(base_url: str, page_number: int) -> str:
        return base_url if page_number == 1 else f"{base_url}/{page_number}"

    def read_page(
        self,
        source: str,
        page_number: int,
        retention_hours: int,
        now: datetime | None = None,
    ) -> ListingPage:
        if self._page is None:
            raise RuntimeError("listing client is not open")
        self._deadline.ensure_available()
        started = time.perf_counter()
        page = self._page
        source_urls = dict(SOURCE_LISTS)
        try:
            base_url = source_urls[source]
        except KeyError as error:
            raise ValueError(f"지원하지 않는 IssueLink 수집처입니다: {source}") from error
        url = self._page_url(base_url, page_number)
        for attempt in range(1, LISTING_LOAD_ATTEMPTS + 1):
            status: int | None = None
            try:
                response = page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=self._deadline.timeout_milliseconds(30),
                )
                status = response.status if response is not None else None
                page.locator(LISTING_LINK_SELECTOR).first.wait_for(
                    state="visible",
                    timeout=self._deadline.timeout_milliseconds(30),
                )
                break
            except PlaywrightError as error:
                self._log_listing_failure(
                    source=source,
                    page_number=page_number,
                    url=url,
                    attempt=attempt,
                    status=status,
                    error=error,
                    elapsed=time.perf_counter() - started,
                )
                if attempt >= LISTING_LOAD_ATTEMPTS:
                    raise
                self._deadline.sleep(random.uniform(*LISTING_RETRY_DELAY_SECONDS))
        self._log_browser_state(
            "목록 로드",
            source=source,
            page_number=page_number,
            elapsed=time.perf_counter() - started,
        )
        raw_rows = page.locator("table tr").evaluate_all(
            """rows => rows.map(row => {
                const link = row.querySelector("a[href*='/community/go/']");
                if (!link) return null;
                return {
                    href: link.getAttribute("href") || "",
                    title: link.textContent?.trim() || "",
                    date: row.querySelector(".second_date span")?.textContent?.trim() || "",
                    hits: row.querySelector(".hit")?.textContent?.trim() || ""
                };
            }).filter(Boolean)"""
        )

        return parse_listing_rows(
            raw_rows,
            retention_hours,
            ISSUELINK_ORIGIN,
            now,
        )

    def resolve_redirect(self, issue_link: str) -> RedirectResult:
        """Resolve a source URL without loading the source site in a page."""
        if self._redirect_resolver is None:
            raise RuntimeError("listing client is not open")
        return self._redirect_resolver.resolve(issue_link)

    def _log_browser_state(
        self,
        event: str,
        *,
        source: str,
        page_number: int,
        elapsed: float,
    ) -> None:
        if self._page is None:
            return
        self._logger.debug(
            "%s: source=%s page=%s url=%s elapsed=%.2fs remaining=%.1fs "
            "cookies=%s cupid_present=%s",
            event,
            source,
            page_number,
            self._page.url,
            elapsed,
            self._deadline.remaining,
            self._cookie_metadata(),
            self._has_cupid_cookie(),
        )

    def _log_listing_failure(
        self,
        *,
        source: str,
        page_number: int,
        url: str,
        attempt: int,
        status: int | None,
        error: PlaywrightError,
        elapsed: float,
    ) -> None:
        title = ""
        preview = ""
        row_count = -1
        link_count = -1
        if self._page is not None:
            try:
                title = self._page.title()
                text = self._page.locator("body").inner_text(timeout=3000)
                preview = " ".join(text.split())[:DIAGNOSTIC_BODY_BYTES]
                row_count = self._page.locator("table tr").count()
                link_count = self._page.locator(LISTING_LINK_SELECTOR).count()
            except PlaywrightError as diagnostic_error:
                preview = f"<진단 실패: {type(diagnostic_error).__name__}>"
        self._logger.warning(
            "목록 로드 실패: source=%s page=%s attempt=%s/%s request_url=%s "
            "current_url=%s status=%s title=%r rows=%s links=%s "
            "error=%s:%s elapsed=%.2fs remaining=%.1fs body_preview=%r "
            "cookies=%s cupid_present=%s",
            source,
            page_number,
            attempt,
            LISTING_LOAD_ATTEMPTS,
            url,
            self._page.url if self._page is not None else "",
            status,
            title,
            row_count,
            link_count,
            type(error).__name__,
            str(error).splitlines()[0] if str(error) else "",
            elapsed,
            self._deadline.remaining,
            preview,
            self._cookie_metadata(),
            self._has_cupid_cookie(),
        )

    def _cookie_metadata(self) -> list[dict[str, object]]:
        if self._context is None:
            return []
        now = time.time()
        metadata = []
        for cookie in self._context.cookies(ISSUELINK_ORIGIN):
            expires = float(cookie.get("expires", -1))
            metadata.append(
                {
                    "name": str(cookie.get("name", "")),
                    "domain": str(cookie.get("domain", "")),
                    "secure": bool(cookie.get("secure", False)),
                    "session": expires <= 0,
                    "expired": expires > 0 and expires <= now,
                }
            )
        return metadata

    def _has_cupid_cookie(self) -> bool:
        return any(
            str(cookie.get("name", "")).upper() == "CUPID"
            for cookie in self._cookie_metadata()
        )

"""External IssueLink clients with conservative request behavior."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

from playwright.sync_api import Error as PlaywrightError, Page, sync_playwright

from .models import ListingPage, RedirectResult
from .parsing import parse_listing_rows
from .redirects import IssueLinkRedirectResolver
from .settings import (
    BLOCKED_RESOURCE_TYPES,
    ISSUELINK_ORIGIN,
    LIST_URL,
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
    def _page_url(page_number: int) -> str:
        return LIST_URL if page_number == 1 else f"{LIST_URL}/{page_number}"

    def read_page(
        self,
        page_number: int,
        retention_hours: int,
        now: datetime | None = None,
    ) -> ListingPage:
        if self._page is None:
            raise RuntimeError("listing client is not open")
        self._deadline.ensure_available()
        started = time.perf_counter()
        page = self._page
        page.goto(
            self._page_url(page_number),
            wait_until="domcontentloaded",
            timeout=self._deadline.timeout_milliseconds(30),
        )
        page.locator("table tr a[href*='/community/go/']").first.wait_for(
            state="visible",
            timeout=self._deadline.timeout_milliseconds(30),
        )
        self._log_browser_state(
            "목록 로드",
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

    def _log_browser_state(self, event: str, *, page_number: int, elapsed: float) -> None:
        if self._page is None:
            return
        self._logger.debug(
            "%s: page=%s url=%s elapsed=%.2fs remaining=%.1fs "
            "cookies=%s cupid_present=%s",
            event,
            page_number,
            self._page.url,
            elapsed,
            self._deadline.remaining,
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

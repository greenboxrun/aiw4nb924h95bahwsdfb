"""External IssueLink clients with conservative request behavior."""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime
from typing import Any

from playwright.sync_api import Error as PlaywrightError, Page, sync_playwright

from .cookies import IssueLinkCookieInspector
from .models import ListingPage, RedirectResult
from .parsing import parse_listing_rows
from .redirects import IssueLinkRedirectResolver
from .settings import (
    BLOCKED_RESOURCE_TYPES,
    DIAGNOSTIC_BODY_BYTES,
    ISSUELINK_ORIGIN,
    FULL_DIAGNOSTIC_EVERY,
    LISTING_GOTO_TIMEOUT_SECONDS,
    LISTING_LINK_SELECTOR,
    LISTING_LINK_TIMEOUT_SECONDS,
    RETRY_DELAY_SECONDS,
    RETRY_WINDOW_SECONDS,
    SOURCE_LISTS,
    USER_AGENT,
)
from .timing import Deadline


DIAGNOSTIC_TEXT_TIMEOUT_MILLISECONDS = 3000
# Runs in the browser: extract the raw cells of every listing row that links to a post.
EXTRACT_LISTING_ROWS_JS = """rows => rows.map(row => {
    const link = row.querySelector("a[href*='/community/go/']");
    if (!link) return null;
    return {
        href: link.getAttribute("href") || "",
        title: link.textContent?.trim() || "",
        date: row.querySelector(".second_date span")?.textContent?.trim() || "",
        hits: row.querySelector(".hit")?.textContent?.trim() || ""
    };
}).filter(Boolean)"""


def listing_page_url(source: str, page_number: int) -> str:
    try:
        base_url = dict(SOURCE_LISTS)[source]
    except KeyError as error:
        raise ValueError(f"지원하지 않는 IssueLink 수집처입니다: {source}") from error
    return base_url if page_number == 1 else f"{base_url}/{page_number}"


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
        self._cookies: IssueLinkCookieInspector | None = None
        self._redirect_resolver: IssueLinkRedirectResolver | None = None
        self._retry_deadline = Deadline(RETRY_WINDOW_SECONDS)

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
        self._cookies = IssueLinkCookieInspector(self._context)
        self._redirect_resolver = IssueLinkRedirectResolver(
            self._context,
            self._page,
            self._deadline,
            self._retry_deadline,
            self._logger,
        )
        return self

    def __exit__(self, *_: object) -> None:
        closers = (
            (self._context, "close"),
            (self._browser, "close"),
            (self._playwright, "stop"),
        )
        for resource, method in closers:
            if resource is None:
                continue
            try:
                getattr(resource, method)()
            except Exception as error:  # pragma: no cover - cleanup best effort
                self._logger.warning("브라우저 리소스 종료 실패: %s", error)

    @staticmethod
    def _route_unneeded_resources(route: Any) -> None:
        if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
            route.abort()
        else:
            route.continue_()

    def read_page(
        self,
        source: str,
        page_number: int,
        retention_hours: int,
        now: datetime | None = None,
    ) -> ListingPage:
        page = self._require_page()
        self._deadline.ensure_available()
        started = time.perf_counter()
        url = listing_page_url(source, page_number)
        self._load_listing(url, source=source, page_number=page_number, started=started)
        self._log_browser_state(
            "목록 로드",
            source=source,
            page_number=page_number,
            elapsed=time.perf_counter() - started,
        )
        raw_rows = page.locator("table tr").evaluate_all(EXTRACT_LISTING_ROWS_JS)
        return parse_listing_rows(raw_rows, retention_hours, ISSUELINK_ORIGIN, now)

    def resolve_redirect(self, issue_link: str) -> RedirectResult:
        """Resolve a source URL without loading the source site in a page."""
        if self._redirect_resolver is None:
            raise RuntimeError("listing client is not open")
        return self._redirect_resolver.resolve(issue_link)

    def _require_page(self) -> Page:
        if self._page is None:
            raise RuntimeError("listing client is not open")
        return self._page

    def _load_listing(
        self,
        url: str,
        *,
        source: str,
        page_number: int,
        started: float,
    ) -> None:
        """Load a listing until its links show, retrying within the retry window."""
        page = self._require_page()
        attempt = 0
        while True:
            attempt += 1
            status: int | None = None
            try:
                response = page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=self._deadline.timeout_milliseconds(LISTING_GOTO_TIMEOUT_SECONDS),
                )
                status = response.status if response is not None else None
                # An error page never shows listing links, so retry without waiting for them.
                if status is not None and status >= 400:
                    raise PlaywrightError(f"listing responded with HTTP {status}")
                page.locator(LISTING_LINK_SELECTOR).first.wait_for(
                    state="visible",
                    timeout=self._deadline.timeout_milliseconds(LISTING_LINK_TIMEOUT_SECONDS),
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
                # Transient failures recover on reload, so keep retrying within the window.
                delay = random.uniform(*RETRY_DELAY_SECONDS)
                if self._retry_deadline.remaining <= delay:
                    raise
                self._deadline.sleep(delay)
        if attempt > 1:
            self._logger.info(
                "목록 로드 재시도 성공: source=%s page=%s attempt=%s 대기누적=%.1fs",
                source,
                page_number,
                attempt,
                time.perf_counter() - started,
            )

    def _cookie_metadata(self) -> list[dict[str, object]]:
        return self._cookies.metadata() if self._cookies is not None else []

    def _has_cupid_cookie(self) -> bool:
        return self._cookies.has_clearance() if self._cookies is not None else False

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
        retry_remaining = max(0.0, self._retry_deadline.remaining)
        error_text = str(error).splitlines()[0] if str(error) else ""
        if (attempt - 1) % FULL_DIAGNOSTIC_EVERY != 0:
            self._logger.warning(
                "목록 로드 실패: source=%s page=%s attempt=%s status=%s "
                "error=%s:%s elapsed=%.2fs retry_remaining=%.0fs",
                source,
                page_number,
                attempt,
                status,
                type(error).__name__,
                error_text,
                elapsed,
                retry_remaining,
            )
            return
        title, preview, row_count, link_count = self._page_diagnostics()
        self._logger.warning(
            "목록 로드 실패: source=%s page=%s attempt=%s retry_remaining=%.0fs "
            "request_url=%s current_url=%s status=%s title=%r rows=%s links=%s "
            "error=%s:%s elapsed=%.2fs remaining=%.1fs body_preview=%r "
            "cookies=%s cupid_present=%s",
            source,
            page_number,
            attempt,
            retry_remaining,
            url,
            self._page.url if self._page is not None else "",
            status,
            title,
            row_count,
            link_count,
            type(error).__name__,
            error_text,
            elapsed,
            self._deadline.remaining,
            preview,
            self._cookie_metadata(),
            self._has_cupid_cookie(),
        )

    def _page_diagnostics(self) -> tuple[str, str, int, int]:
        """Return title, body preview, row count and link count of the current page."""
        title = ""
        preview = ""
        row_count = -1
        link_count = -1
        if self._page is None:
            return title, preview, row_count, link_count
        try:
            title = self._page.title()
            text = self._page.locator("body").inner_text(
                timeout=DIAGNOSTIC_TEXT_TIMEOUT_MILLISECONDS
            )
            preview = " ".join(text.split())[:DIAGNOSTIC_BODY_BYTES]
            row_count = self._page.locator("table tr").count()
            link_count = self._page.locator(LISTING_LINK_SELECTOR).count()
        except PlaywrightError as diagnostic_error:
            preview = f"<진단 실패: {type(diagnostic_error).__name__}>"
        return title, preview, row_count, link_count

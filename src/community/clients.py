"""External IssueLink clients with conservative request behavior."""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import requests
from playwright.sync_api import Page, sync_playwright

from .models import ListingCandidate
from .parsing import (
    is_expired,
    parse_identity,
    parse_integer,
    parse_title_and_comments,
)
from .timing import Deadline


LIST_URL = (
    "https://www.issuelink.co.kr/community/listview/all/48/"
    "click/_self/blank/blank/blank"
)
ISSUELINK_ORIGIN = "https://www.issuelink.co.kr"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
BLOCKED_RESOURCE_TYPES = {"font", "image", "media", "stylesheet"}
MAX_REDIRECT_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class ListingPage:
    candidates: list[ListingCandidate]
    expired_count: int


class IssueLinkListingClient:
    """Read listing pages through one lightweight Playwright session."""

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

    def read_page(self, page_number: int, retention_hours: int) -> ListingPage:
        if self._page is None:
            raise RuntimeError("listing client is not open")
        self._deadline.ensure_available()
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

        candidates: list[ListingCandidate] = []
        expired_count = 0
        for raw_row in raw_rows:
            href = str(raw_row.get("href", ""))
            date = str(raw_row.get("date", ""))
            if is_expired({"작성시간": date}, retention_hours):
                expired_count += 1
                continue
            identity = parse_identity(href)
            if identity is None:
                continue
            site, post_id = identity
            title, comments = parse_title_and_comments(str(raw_row.get("title", "")))
            candidates.append(
                ListingCandidate(
                    site=site,
                    post_id=post_id,
                    title=title,
                    written_at=date,
                    comment_count=comments,
                    view_count=parse_integer(str(raw_row.get("hits", ""))),
                    issue_link=urljoin(ISSUELINK_ORIGIN, href),
                )
            )
        return ListingPage(candidates=candidates, expired_count=expired_count)


class IssueLinkRedirectClient:
    """Resolve IssueLink redirects without requesting the original sites."""

    def __init__(self, deadline: Deadline, logger: logging.Logger) -> None:
        self._deadline = deadline
        self._logger = logger
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept-Language": "ko-KR,ko;q=0.9",
            }
        )

    def __enter__(self) -> "IssueLinkRedirectClient":
        return self

    def __exit__(self, *_: object) -> None:
        self._session.close()

    def resolve(self, issue_link: str) -> str | None:
        for attempt in range(MAX_REDIRECT_ATTEMPTS):
            self._deadline.ensure_available()
            try:
                timeout = (
                    self._deadline.timeout_seconds(5),
                    self._deadline.timeout_seconds(20),
                )
                response = self._session.get(
                    issue_link,
                    timeout=timeout,
                    allow_redirects=False,
                )
                location = response.headers.get("Location", "").strip()
                if not location:
                    self._logger.warning(
                        "원문 주소 없음, 건너뜀: %s (Location 헤더 없음)", issue_link
                    )
                    return None
                return urljoin(issue_link, location)
            except requests.RequestException as error:
                if attempt + 1 == MAX_REDIRECT_ATTEMPTS:
                    self._logger.warning(
                        "IssueLink 주소 요청 실패, 건너뜀: %s (%s)", issue_link, error
                    )
                    return None
                delay = random.uniform(0.7, 1.1) * (2**attempt)
                self._logger.warning(
                    "IssueLink 주소 요청 재시도 %s/%s: %s (%s), %.1f초 대기",
                    attempt + 1,
                    MAX_REDIRECT_ATTEMPTS - 1,
                    issue_link,
                    error,
                    delay,
                )
                self._deadline.sleep(delay)
        return None

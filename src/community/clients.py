"""External IssueLink clients with conservative request behavior."""

from __future__ import annotations

import hashlib
import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

from playwright.sync_api import APIRequestContext, Error as PlaywrightError, Page, sync_playwright

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
MAX_REDIRECT_ATTEMPTS = 2
DIAGNOSTIC_BODY_BYTES = 2048


@dataclass(frozen=True, slots=True)
class ListingPage:
    candidates: list[ListingCandidate]
    expired_count: int


@dataclass(frozen=True, slots=True)
class RedirectResult:
    original_url: str | None
    challenge_count: int = 0
    clearance_refreshes: int = 0
    network_errors: int = 0


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
        self._clearance_refresh_used = False

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

    def resolve_redirect(self, issue_link: str) -> RedirectResult:
        """Resolve a source URL without loading the source site in a page."""
        if self._context is None:
            raise RuntimeError("listing client is not open")

        challenge_count = 0
        clearance_refreshes = 0
        network_errors = 0
        request: APIRequestContext = self._context.request

        for attempt in range(1, MAX_REDIRECT_ATTEMPTS + 1):
            self._deadline.ensure_available()
            started = time.perf_counter()
            try:
                response = request.get(
                    issue_link,
                    timeout=self._deadline.timeout_milliseconds(20),
                    max_redirects=0,
                    headers={
                        "Accept": (
                            "text/html,application/xhtml+xml,application/xml;q=0.9,"
                            "*/*;q=0.8"
                        ),
                        "Accept-Language": "ko-KR,ko;q=0.9",
                        "Referer": ISSUELINK_ORIGIN + "/",
                    },
                )
            except PlaywrightError as error:
                network_errors += 1
                self._logger.warning(
                    "원문 요청 네트워크 오류: transport=playwright_api url=%s "
                    "attempt=%s/%s error=%s:%s elapsed=%.2fs remaining=%.1fs",
                    issue_link,
                    attempt,
                    MAX_REDIRECT_ATTEMPTS,
                    type(error).__name__,
                    error,
                    time.perf_counter() - started,
                    self._deadline.remaining,
                )
                if attempt < MAX_REDIRECT_ATTEMPTS:
                    self._deadline.sleep(random.uniform(0.7, 1.1))
                    continue
                return RedirectResult(
                    None,
                    challenge_count,
                    clearance_refreshes,
                    network_errors,
                )

            try:
                headers = {
                    str(key).lower(): str(value)
                    for key, value in response.headers.items()
                }
                body = response.body()
                body_text = body.decode("utf-8", errors="replace")
                location = headers.get("location", "").strip()
                challenge = _detect_challenge(body_text, headers)
                elapsed = time.perf_counter() - started
                set_cookie_names = _set_cookie_names(headers.get("set-cookie", ""))
                self._logger.debug(
                    "원문 요청 응답: transport=playwright_api url=%s attempt=%s/%s "
                    "status=%s location=%s challenge=%s body_bytes=%s elapsed=%.2fs "
                    "remaining=%.1fs headers=%s set_cookie_names=%s",
                    issue_link,
                    attempt,
                    MAX_REDIRECT_ATTEMPTS,
                    response.status,
                    location or "<없음>",
                    challenge or "none",
                    len(body),
                    elapsed,
                    self._deadline.remaining,
                    _diagnostic_headers(headers),
                    set_cookie_names,
                )

                if challenge:
                    challenge_count += 1
                    self._log_failed_response(
                        issue_link,
                        attempt,
                        response.status,
                        headers,
                        body,
                        challenge,
                    )
                    if not self._clearance_refresh_used and attempt < MAX_REDIRECT_ATTEMPTS:
                        self._clearance_refresh_used = True
                        clearance_refreshes += 1
                        try:
                            self._refresh_clearance()
                        except PlaywrightError as error:
                            network_errors += 1
                            self._logger.warning(
                                "CUPID 쿠키 갱신 실패: error=%s:%s remaining=%.1fs",
                                type(error).__name__,
                                error,
                                self._deadline.remaining,
                            )
                            return RedirectResult(
                                None,
                                challenge_count,
                                clearance_refreshes,
                                network_errors,
                            )
                        continue
                    self._logger.warning(
                        "CUPID 챌린지 재시도 중단: url=%s clearance_refresh=%s",
                        issue_link,
                        "already_used" if self._clearance_refresh_used else "unavailable",
                    )
                    return RedirectResult(
                        None,
                        challenge_count,
                        clearance_refreshes,
                        network_errors,
                    )

                resolved = extract_redirect_url(issue_link, headers, body_text)
                if resolved and not _is_issuelink_go_url(resolved):
                    return RedirectResult(
                        resolved,
                        challenge_count,
                        clearance_refreshes,
                        network_errors,
                    )

                self._log_failed_response(
                    issue_link,
                    attempt,
                    response.status,
                    headers,
                    body,
                    "none",
                )
                self._logger.warning(
                    "원문 리다이렉트 없음, 재시도하지 않음: url=%s status=%s",
                    issue_link,
                    response.status,
                )
                return RedirectResult(
                    None,
                    challenge_count,
                    clearance_refreshes,
                    network_errors,
                )
            finally:
                response.dispose()

        return RedirectResult(
            None,
            challenge_count,
            clearance_refreshes,
            network_errors,
        )

    def _refresh_clearance(self) -> None:
        if self._page is None:
            raise RuntimeError("listing client is not open")
        before = self._cookie_metadata()
        page = self._page
        target_url = page.url if page.url.startswith(ISSUELINK_ORIGIN) else LIST_URL
        started = time.perf_counter()
        self._logger.warning(
            "CUPID 쿠키 갱신 시작: url=%s cookies_before=%s remaining=%.1fs",
            target_url,
            before,
            self._deadline.remaining,
        )
        page.goto(
            target_url,
            wait_until="domcontentloaded",
            timeout=self._deadline.timeout_milliseconds(30),
        )
        page.locator("table tr a[href*='/community/go/']").first.wait_for(
            state="visible",
            timeout=self._deadline.timeout_milliseconds(30),
        )
        self._logger.warning(
            "CUPID 쿠키 갱신 완료: url=%s elapsed=%.2fs cookies_after=%s "
            "cupid_present=%s remaining=%.1fs",
            page.url,
            time.perf_counter() - started,
            self._cookie_metadata(),
            self._has_cupid_cookie(),
            self._deadline.remaining,
        )

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

    def _log_failed_response(
        self,
        issue_link: str,
        attempt: int,
        status: int,
        headers: dict[str, str],
        body: bytes,
        challenge: str,
    ) -> None:
        preview = " ".join(
            body[:DIAGNOSTIC_BODY_BYTES].decode("utf-8", errors="replace").split()
        )
        self._logger.warning(
            "원문 응답 진단: transport=playwright_api url=%s attempt=%s/%s "
            "status=%s challenge=%s body_bytes=%s body_sha256=%s "
            "body_preview=%r headers=%s set_cookie_names=%s cookies=%s "
            "cupid_present=%s remaining=%.1fs",
            issue_link,
            attempt,
            MAX_REDIRECT_ATTEMPTS,
            status,
            challenge,
            len(body),
            hashlib.sha256(body).hexdigest(),
            preview,
            _diagnostic_headers(headers),
            _set_cookie_names(headers.get("set-cookie", "")),
            self._cookie_metadata(),
            self._has_cupid_cookie(),
            self._deadline.remaining,
        )


def extract_redirect_url(
    response_url: str,
    headers: dict[str, str],
    body: str,
) -> str | None:
    """Extract a redirect target without following it to the source site."""
    location = headers.get("location", "").strip()
    if location:
        return urljoin(response_url, location)

    refresh = headers.get("refresh", "")
    refresh_match = re.search(r"(?:^|;)\s*url\s*=\s*([^;]+)", refresh, re.IGNORECASE)
    if refresh_match:
        return urljoin(response_url, refresh_match.group(1).strip(" '\""))

    patterns = (
        r"<meta[^>]+http-equiv\s*=\s*['\"]?refresh['\"]?[^>]+content\s*=\s*['\"][^'\"]*url\s*=\s*([^'\"]+)",
        r"(?:window\.)?location(?:\.href|\.replace|\.assign)?\s*\(?'?\s*['\"]([^'\"]+)['\"]",
    )
    for pattern in patterns:
        match = re.search(pattern, body, re.IGNORECASE)
        if match:
            return urljoin(response_url, match.group(1).strip())
    return None


def _detect_challenge(body: str, headers: dict[str, str] | None = None) -> str | None:
    lowered = body.lower()
    markers = ("cupid.js", "slowaes.decrypt", 'document.cookie="cupid=', "cupid=")
    header_text = ""
    if headers:
        header_text = " ".join(
            f"{key}:{value}" for key, value in headers.items()
        ).lower()
    return "cupid" if any(marker in lowered for marker in markers) or "cupid" in header_text else None


def _is_issuelink_go_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.netloc.lower().endswith("issuelink.co.kr") and parsed.path.startswith(
        "/community/go/"
    )


def _set_cookie_names(header: str) -> list[str]:
    if not header:
        return []
    return sorted(
        set(
            re.findall(
                r"(?:^|,\s*)([!#$%&'*+\-.^_`|~0-9A-Za-z]+)=",
                header,
            )
        )
    )


def _diagnostic_headers(headers: Any) -> dict[str, str]:
    """Keep useful response headers while excluding cookies and auth values."""
    names = (
        "location",
        "refresh",
        "content-type",
        "server",
        "via",
        "x-cache",
        "x-cache-hits",
        "cf-cache-status",
        "retry-after",
    )
    lowered = {str(key).lower(): str(value) for key, value in headers.items()}
    return {name: lowered[name] for name in names if name in lowered}

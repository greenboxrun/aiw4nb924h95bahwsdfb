"""IssueLink redirect resolution with conservative challenge handling."""

from __future__ import annotations

import hashlib
import logging
import random
import time
from dataclasses import dataclass

from playwright.sync_api import APIResponse, BrowserContext, Error as PlaywrightError, Page

from .cookies import IssueLinkCookieInspector
from .models import RedirectResult
from .response_policy import (
    detect_challenge,
    diagnostic_headers,
    extract_redirect_url,
    is_issuelink_go_url,
    set_cookie_names,
)
from .settings import (
    DIAGNOSTIC_BODY_BYTES,
    ISSUELINK_ORIGIN,
    LISTING_LINK_SELECTOR,
    MAX_REDIRECT_ATTEMPTS,
    SOURCE_LISTS,
)
from .timing import Deadline


REDIRECT_REQUEST_TIMEOUT_SECONDS = 20
CLEARANCE_REFRESH_TIMEOUT_SECONDS = 30
NETWORK_RETRY_DELAY_SECONDS = (0.7, 1.1)
REDIRECT_REQUEST_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Referer": ISSUELINK_ORIGIN + "/",
}


@dataclass(slots=True)
class _RedirectCounters:
    challenge_count: int = 0
    clearance_refreshes: int = 0
    network_errors: int = 0

    def result(self, original_url: str | None) -> RedirectResult:
        return RedirectResult(
            original_url,
            self.challenge_count,
            self.clearance_refreshes,
            self.network_errors,
        )


@dataclass(frozen=True, slots=True)
class _ResponseSnapshot:
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    @classmethod
    def read(cls, response: APIResponse) -> "_ResponseSnapshot":
        try:
            headers = {
                str(key).lower(): str(value)
                for key, value in response.headers.items()
            }
            return cls(response.status, headers, response.body())
        finally:
            response.dispose()


class IssueLinkRedirectResolver:
    """Resolve IssueLink URLs without requesting the original source site."""

    def __init__(
        self,
        context: BrowserContext,
        page: Page,
        deadline: Deadline,
        logger: logging.Logger,
    ) -> None:
        self._context = context
        self._page = page
        self._deadline = deadline
        self._logger = logger
        self._cookies = IssueLinkCookieInspector(context)
        self._clearance_refresh_used = False

    def resolve(self, issue_link: str) -> RedirectResult:
        """Read only the redirect response and return its original URL."""
        counters = _RedirectCounters()

        for attempt in range(1, MAX_REDIRECT_ATTEMPTS + 1):
            self._deadline.ensure_available()
            started = time.perf_counter()
            try:
                response = self._context.request.get(
                    issue_link,
                    timeout=self._deadline.timeout_milliseconds(
                        REDIRECT_REQUEST_TIMEOUT_SECONDS
                    ),
                    max_redirects=0,
                    headers=REDIRECT_REQUEST_HEADERS,
                )
            except PlaywrightError as error:
                counters.network_errors += 1
                self._log_network_error(issue_link, attempt, error, started)
                if attempt < MAX_REDIRECT_ATTEMPTS:
                    self._deadline.sleep(random.uniform(*NETWORK_RETRY_DELAY_SECONDS))
                    continue
                return counters.result(None)

            snapshot = _ResponseSnapshot.read(response)
            challenge = detect_challenge(snapshot.text, snapshot.headers)
            self._log_response(issue_link, attempt, snapshot, challenge, started)

            if challenge:
                counters.challenge_count += 1
                self._log_failed_response(issue_link, attempt, snapshot, challenge)
                if self._try_refresh_clearance(issue_link, attempt, counters):
                    continue
                return counters.result(None)

            resolved = extract_redirect_url(issue_link, snapshot.headers, snapshot.text)
            if resolved and not is_issuelink_go_url(resolved):
                return counters.result(resolved)

            self._log_failed_response(issue_link, attempt, snapshot, "none")
            self._logger.warning(
                "원문 리다이렉트 없음, 재시도하지 않음: url=%s status=%s",
                issue_link,
                snapshot.status,
            )
            return counters.result(None)

        return counters.result(None)

    def _try_refresh_clearance(
        self,
        issue_link: str,
        attempt: int,
        counters: _RedirectCounters,
    ) -> bool:
        """Refresh the CUPID cookie once per crawl; return whether to retry."""
        if self._clearance_refresh_used or attempt >= MAX_REDIRECT_ATTEMPTS:
            self._logger.warning(
                "CUPID 챌린지 재시도 중단: url=%s clearance_refresh=%s",
                issue_link,
                "already_used" if self._clearance_refresh_used else "unavailable",
            )
            return False
        self._clearance_refresh_used = True
        counters.clearance_refreshes += 1
        try:
            self._refresh_clearance()
        except PlaywrightError as error:
            counters.network_errors += 1
            self._logger.warning(
                "CUPID 쿠키 갱신 실패: error=%s:%s remaining=%.1fs",
                type(error).__name__,
                error,
                self._deadline.remaining,
            )
            return False
        return True

    def _refresh_clearance(self) -> None:
        started = time.perf_counter()
        target_url = (
            self._page.url
            if self._page.url.startswith(ISSUELINK_ORIGIN)
            else SOURCE_LISTS[0][1]
        )
        self._logger.warning(
            "CUPID 쿠키 갱신 시작: url=%s cookies_before=%s remaining=%.1fs",
            target_url,
            self._cookies.metadata(),
            self._deadline.remaining,
        )
        self._page.goto(
            target_url,
            wait_until="domcontentloaded",
            timeout=self._deadline.timeout_milliseconds(CLEARANCE_REFRESH_TIMEOUT_SECONDS),
        )
        self._page.locator(LISTING_LINK_SELECTOR).first.wait_for(
            state="visible",
            timeout=self._deadline.timeout_milliseconds(CLEARANCE_REFRESH_TIMEOUT_SECONDS),
        )
        self._logger.warning(
            "CUPID 쿠키 갱신 완료: url=%s elapsed=%.2fs cookies_after=%s "
            "cupid_present=%s remaining=%.1fs",
            self._page.url,
            time.perf_counter() - started,
            self._cookies.metadata(),
            self._cookies.has_clearance(),
            self._deadline.remaining,
        )

    def _log_network_error(
        self,
        issue_link: str,
        attempt: int,
        error: PlaywrightError,
        started: float,
    ) -> None:
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

    def _log_response(
        self,
        issue_link: str,
        attempt: int,
        snapshot: _ResponseSnapshot,
        challenge: str | None,
        started: float,
    ) -> None:
        self._logger.debug(
            "원문 요청 응답: transport=playwright_api url=%s attempt=%s/%s "
            "status=%s location=%s challenge=%s body_bytes=%s elapsed=%.2fs "
            "remaining=%.1fs headers=%s set_cookie_names=%s",
            issue_link,
            attempt,
            MAX_REDIRECT_ATTEMPTS,
            snapshot.status,
            snapshot.headers.get("location", "").strip() or "<없음>",
            challenge or "none",
            len(snapshot.body),
            time.perf_counter() - started,
            self._deadline.remaining,
            diagnostic_headers(snapshot.headers),
            set_cookie_names(snapshot.headers.get("set-cookie", "")),
        )

    def _log_failed_response(
        self,
        issue_link: str,
        attempt: int,
        snapshot: _ResponseSnapshot,
        challenge: str,
    ) -> None:
        body = snapshot.body
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
            snapshot.status,
            challenge,
            len(body),
            hashlib.sha256(body).hexdigest(),
            preview,
            diagnostic_headers(snapshot.headers),
            set_cookie_names(snapshot.headers.get("set-cookie", "")),
            self._cookies.metadata(),
            self._cookies.has_clearance(),
            self._deadline.remaining,
        )

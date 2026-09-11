"""IssueLink redirect resolution with conservative challenge handling."""

from __future__ import annotations

import hashlib
import logging
import random
import time
from typing import Any

from playwright.sync_api import APIRequestContext, BrowserContext, Error as PlaywrightError, Page

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
    LIST_URL,
    MAX_REDIRECT_ATTEMPTS,
)
from .timing import Deadline


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
        self._clearance_refresh_used = False

    def resolve(self, issue_link: str) -> RedirectResult:
        """Read only the redirect response and return its original URL."""
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
                challenge = detect_challenge(body_text, headers)
                elapsed = time.perf_counter() - started
                cookie_names = set_cookie_names(headers.get("set-cookie", ""))
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
                    diagnostic_headers(headers),
                    cookie_names,
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
                if resolved and not is_issuelink_go_url(resolved):
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
        started = time.perf_counter()
        before = self._cookie_metadata()
        target_url = self._page.url if self._page.url.startswith(ISSUELINK_ORIGIN) else LIST_URL
        self._logger.warning(
            "CUPID 쿠키 갱신 시작: url=%s cookies_before=%s remaining=%.1fs",
            target_url,
            before,
            self._deadline.remaining,
        )
        self._page.goto(
            target_url,
            wait_until="domcontentloaded",
            timeout=self._deadline.timeout_milliseconds(30),
        )
        self._page.locator("table tr a[href*='/community/go/']").first.wait_for(
            state="visible",
            timeout=self._deadline.timeout_milliseconds(30),
        )
        self._logger.warning(
            "CUPID 쿠키 갱신 완료: url=%s elapsed=%.2fs cookies_after=%s "
            "cupid_present=%s remaining=%.1fs",
            self._page.url,
            time.perf_counter() - started,
            self._cookie_metadata(),
            self._has_cupid_cookie(),
            self._deadline.remaining,
        )

    def _cookie_metadata(self) -> list[dict[str, object]]:
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
            diagnostic_headers(headers),
            set_cookie_names(headers.get("set-cookie", "")),
            self._cookie_metadata(),
            self._has_cupid_cookie(),
            self._deadline.remaining,
        )

"""Cookie diagnostics for the IssueLink browser context."""

from __future__ import annotations

import time

from playwright.sync_api import BrowserContext

from .settings import ISSUELINK_ORIGIN


CLEARANCE_COOKIE_NAME = "CUPID"


class IssueLinkCookieInspector:
    """Describe IssueLink cookies without exposing their values in logs."""

    def __init__(self, context: BrowserContext) -> None:
        self._context = context

    def metadata(self) -> list[dict[str, object]]:
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

    def has_clearance(self) -> bool:
        return any(
            str(cookie["name"]).upper() == CLEARANCE_COOKIE_NAME
            for cookie in self.metadata()
        )

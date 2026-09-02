"""Execution deadline helpers."""

from __future__ import annotations

import time


class CrawlDeadlineExceeded(RuntimeError):
    """Raised when the cooperative crawl deadline has been reached."""


class Deadline:
    """Monotonic deadline shared by browser and HTTP operations."""

    def __init__(self, limit_seconds: float | None = None) -> None:
        if limit_seconds is not None and limit_seconds <= 0:
            raise ValueError("limit_seconds must be greater than zero")
        self._expires_at = (
            time.monotonic() + limit_seconds
            if limit_seconds is not None
            else float("inf")
        )

    @property
    def remaining(self) -> float:
        return self._expires_at - time.monotonic()

    def ensure_available(self) -> None:
        if self.remaining <= 0:
            raise CrawlDeadlineExceeded("crawl execution deadline reached")

    def timeout_milliseconds(self, maximum_seconds: float) -> int:
        self.ensure_available()
        return max(100, int(min(maximum_seconds, self.remaining) * 1000))

    def timeout_seconds(self, maximum_seconds: float) -> float:
        self.ensure_available()
        return max(0.1, min(maximum_seconds, self.remaining))

    def sleep(self, seconds: float) -> None:
        self.ensure_available()
        time.sleep(min(seconds, max(0.0, self.remaining)))
        self.ensure_available()

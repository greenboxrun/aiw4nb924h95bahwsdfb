"""Interfaces used by the community crawl application service."""

from __future__ import annotations

import logging
from contextlib import AbstractContextManager
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from .models import ListingPage, RedirectResult
from .timing import Deadline


class RecordRepository(Protocol):
    """Persistence boundary for the crawl snapshot."""

    path: Path

    def load(self) -> list[dict[str, Any]]:
        """Load the current snapshot."""

    def save(self, records: list[dict[str, Any]]) -> None:
        """Persist the next snapshot."""


class ListingClient(Protocol):
    """Boundary for reading IssueLink pages and resolving source URLs."""

    def __enter__(self) -> "ListingClient":
        """Open the client resources."""

    def __exit__(self, *_: object) -> None:
        """Release the client resources."""

    def read_page(
        self,
        source: str,
        page_number: int,
        retention_hours: int,
        now: datetime | None = None,
    ) -> ListingPage:
        """Read one listing page."""

    def resolve_redirect(self, issue_link: str) -> RedirectResult:
        """Resolve one IssueLink URL to its original URL."""


class ListingClientFactory(Protocol):
    """Factory for a configured listing client context manager."""

    def __call__(
        self,
        deadline: Deadline,
        logger: logging.Logger,
        *,
        headed: bool = False,
    ) -> AbstractContextManager[ListingClient]:
        """Create a listing client for one crawl run."""

"""Pure parsing and retention rules for IssueLink data."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse


# South Korea uses a fixed UTC+09:00 offset and does not observe DST.
KST = timezone(timedelta(hours=9), "KST")


def parse_identity(href: str) -> tuple[str, str] | None:
    """Extract the source site and post ID from an IssueLink URL."""
    parts = [part for part in urlparse(href).path.split("/") if part]
    try:
        community_index = parts.index("community")
        if parts[community_index + 1] != "go":
            return None
        site = parts[community_index + 2]
        post_id = parts[community_index + 3]
    except (ValueError, IndexError):
        return None
    return (site, post_id) if site and post_id else None


def parse_integer(value: str) -> int:
    """Parse a number containing commas or other display characters."""
    return int(re.sub(r"[^0-9]", "", value) or 0)


def parse_title_and_comments(title: str) -> tuple[str, int]:
    match = re.search(r"\s*\[\s*([0-9][0-9,]*)\s*\]\s*$", title)
    if match is None:
        return title.strip(), 0
    return title[: match.start()].strip(), parse_integer(match.group(1))


def parse_written_at(value: str) -> datetime | None:
    """Parse the absolute date formats used by IssueLink listings."""
    value = value.strip()
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y.%m.%d %H:%M"):
        try:
            return datetime.strptime(value, pattern).replace(tzinfo=KST)
        except ValueError:
            continue
    return None


def record_key(record: dict[str, Any]) -> tuple[str, str] | None:
    site = str(record.get("사이트", "")).strip()
    post_id = str(record.get("id값", "")).strip()
    return (site, post_id) if site and post_id else None


def is_expired(
    record: dict[str, Any], retention_hours: int, now: datetime | None = None
) -> bool:
    """Return whether a record is older than the configured retention period."""
    written_at = parse_written_at(str(record.get("작성시간", "")))
    if written_at is None:
        # Records without a recognized absolute timestamp cannot be proven to
        # be within the KST retention window.
        return True
    reference_time = now or datetime.now(KST)
    if reference_time.tzinfo is None:
        reference_time = reference_time.replace(tzinfo=KST)
    else:
        reference_time = reference_time.astimezone(KST)
    return written_at < reference_time - timedelta(hours=retention_hours)


def remove_expired(
    records: list[dict[str, Any]], retention_hours: int, now: datetime | None = None
) -> list[dict[str, Any]]:
    return [
        record
        for record in records
        if not is_expired(record, retention_hours, now)
    ]

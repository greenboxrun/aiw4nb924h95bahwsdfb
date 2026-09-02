"""Domain models used by the IssueLink community crawler."""

from __future__ import annotations

from dataclasses import dataclass


OUTPUT_FIELDS = ("사이트", "id값", "제목", "작성시간", "댓글수", "조회수", "원문URL")


@dataclass(frozen=True, slots=True)
class ListingCandidate:
    """A post extracted from an IssueLink listing page."""

    site: str
    post_id: str
    title: str
    written_at: str
    comment_count: int
    view_count: int
    issue_link: str

    @property
    def key(self) -> tuple[str, str]:
        return self.site, self.post_id

    def to_record(self, original_url: str) -> dict[str, object]:
        return {
            "사이트": self.site,
            "id값": self.post_id,
            "제목": self.title,
            "작성시간": self.written_at,
            "댓글수": self.comment_count,
            "조회수": self.view_count,
            "원문URL": original_url,
        }


@dataclass(slots=True)
class CrawlStats:
    """Counters emitted at the end of one crawl run."""

    listed: int = 0
    saved: int = 0
    updated: int = 0
    duplicates: int = 0
    expired_skipped: int = 0
    request_failures: int = 0

"""Domain models used by the IssueLink community crawler."""

from __future__ import annotations

from dataclasses import dataclass

from .settings import SOURCE_LISTS


SITE_FIELD = "사이트"
POST_ID_FIELD = "id값"
TITLE_FIELD = "제목"
WRITTEN_AT_FIELD = "작성시간"
COMMENT_COUNT_FIELD = "댓글수"
VIEW_COUNT_FIELD = "조회수"
ORIGINAL_URL_FIELD = "원문URL"
SOURCE_FIELD = "수집처"
OUTPUT_FIELDS = (
    SITE_FIELD,
    POST_ID_FIELD,
    TITLE_FIELD,
    WRITTEN_AT_FIELD,
    COMMENT_COUNT_FIELD,
    VIEW_COUNT_FIELD,
    ORIGINAL_URL_FIELD,
    SOURCE_FIELD,
)

ISSUELINK_SOURCE_ORDER = tuple(name for name, _ in SOURCE_LISTS)
# Source names written by earlier crawler versions; replaced whenever a record is refreshed.
ISSUELINK_LEGACY_SOURCES = frozenset(("issuelink", "issuelink_adj", "issuelink_click"))
ISSUELINK_SOURCES = frozenset((*ISSUELINK_SOURCE_ORDER, *ISSUELINK_LEGACY_SOURCES))


@dataclass(frozen=True, slots=True)
class ListingPage:
    """Parsed posts and rows skipped by the retention policy."""

    candidates: list["ListingCandidate"]
    expired_count: int


@dataclass(frozen=True, slots=True)
class RedirectResult:
    """Outcome and diagnostics from resolving one IssueLink redirect."""

    original_url: str | None
    challenge_count: int = 0
    clearance_refreshes: int = 0
    network_errors: int = 0


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

    def to_record(
        self,
        original_url: str,
        sources: list[dict[str, object]],
    ) -> dict[str, object]:
        return {
            SITE_FIELD: self.site,
            POST_ID_FIELD: self.post_id,
            TITLE_FIELD: self.title,
            WRITTEN_AT_FIELD: self.written_at,
            COMMENT_COUNT_FIELD: self.comment_count,
            VIEW_COUNT_FIELD: self.view_count,
            ORIGINAL_URL_FIELD: original_url,
            SOURCE_FIELD: sources,
        }


@dataclass(slots=True)
class CrawlStats:
    """Counters emitted at the end of one crawl run."""

    listed: int = 0
    saved: int = 0
    cached_url_hits: int = 0
    duplicates: int = 0
    expired_skipped: int = 0
    redirect_successes: int = 0
    cupid_challenges: int = 0
    clearance_refreshes: int = 0
    network_errors: int = 0
    request_failures: int = 0
    listing_failures: int = 0
    carried_over: int = 0
    failed_sources: tuple[str, ...] = ()

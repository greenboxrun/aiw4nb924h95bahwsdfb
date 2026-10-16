"""Application service for the realtime IssueLink crawler."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from playwright.sync_api import Error as PlaywrightError

from .clients import IssueLinkListingClient
from .models import POST_ID_FIELD, SITE_FIELD, CrawlStats, ListingCandidate
from .parsing import KST
from .ports import ListingClient, ListingClientFactory, RecordRepository
from .record_policy import CarryOverCandidate, RecordKey, SnapshotBuilder
from .settings import LISTING_PAGE_DELAY_SECONDS, PAGES_PER_LIST, SOURCE_LISTS
from .timing import Deadline


REDIRECT_DELAY_SECONDS = (0.5, 0.8)


@dataclass(frozen=True, slots=True)
class CrawlerConfig:
    target_total_posts: int = 1000
    max_pages: int = PAGES_PER_LIST
    retention_hours: int = 48
    max_runtime_seconds: float | None = None
    headed: bool = False

    def validate(self) -> None:
        if self.target_total_posts < 1:
            raise ValueError("max-new-posts는 1 이상이어야 합니다.")
        if self.max_pages != PAGES_PER_LIST:
            raise ValueError(f"max-pages는 {PAGES_PER_LIST}이어야 합니다.")
        if self.retention_hours < 0:
            raise ValueError("retention-hours는 0 이상이어야 합니다.")
        if self.max_runtime_seconds is not None and self.max_runtime_seconds <= 0:
            raise ValueError("max-runtime-seconds는 1 이상이어야 합니다.")


@dataclass(slots=True)
class CandidateAggregate:
    """One unique post and its rank in every listing that showed it."""

    candidate: ListingCandidate
    discovery_order: int
    source_scores: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CandidateCollection:
    """Listing candidates plus the lists that failed to load completely."""

    candidates: dict[RecordKey, CandidateAggregate]
    failed_sources: frozenset[str]


@dataclass(frozen=True, slots=True)
class RankedEntry:
    """One fresh candidate or carried record competing for a snapshot slot."""

    score: int
    order: tuple[int, int]
    aggregate: CandidateAggregate | None = None
    carried: CarryOverCandidate | None = None


class RealtimeCrawler:
    """Build a fresh listing snapshot while reusing cached original URLs."""

    def __init__(
        self,
        repository: RecordRepository,
        config: CrawlerConfig,
        logger: logging.Logger,
        listing_client_factory: ListingClientFactory = IssueLinkListingClient,
    ) -> None:
        config.validate()
        self._repository = repository
        self._config = config
        self._logger = logger
        self._listing_client_factory = listing_client_factory

    def run(self) -> CrawlStats:
        started = time.perf_counter()
        deadline = Deadline(self._config.max_runtime_seconds)
        reference_time = datetime.now(KST)
        cached_records = self._repository.load()
        snapshot = SnapshotBuilder(cached_records)
        self._logger.info(
            "기존 JSON 원문 URL 캐시: 레코드 %s개, URL %s개",
            len(cached_records),
            snapshot.cached_url_count,
        )

        stats = CrawlStats()
        with self._listing_client_factory(
            deadline,
            self._logger,
            headed=self._config.headed,
        ) as listings:
            self._crawl(listings, snapshot, stats, deadline, reference_time)

        self._repository.save(snapshot.records)
        self._log_summary(stats, snapshot, time.perf_counter() - started)
        return stats

    def _crawl(
        self,
        listings: ListingClient,
        snapshot: SnapshotBuilder,
        stats: CrawlStats,
        deadline: Deadline,
        reference_time: datetime,
    ) -> None:
        collection = self._collect_candidates(listings, stats, deadline, reference_time)
        failed_sources = collection.failed_sources
        stats.failed_sources = tuple(sorted(failed_sources))
        carried = snapshot.carry_over_candidates(
            failed_sources,
            collection.candidates.keys(),
            self._config.retention_hours,
            reference_time,
        )
        if failed_sources:
            self._logger.warning(
                "목록 로드 실패 수집처는 직전 결과로 보완: failed=%s 이어붙일 후보=%s개",
                sorted(failed_sources),
                len(carried),
            )
        ranked_entries = self._rank_entries(
            collection.candidates,
            carried,
            snapshot,
            failed_sources,
        )
        self._logger.info(
            "후보 병합 완료: 고유 %s개, 이어붙일 후보 %s개, 정렬 후보 %s개",
            len(collection.candidates),
            len(carried),
            len(ranked_entries),
        )

        for entry in ranked_entries:
            deadline.ensure_available()
            if snapshot.size >= self._config.target_total_posts:
                break
            if entry.carried is not None:
                self._add_carried(entry.score, entry.carried, snapshot, stats)
            elif entry.aggregate is not None:
                self._add_fresh(
                    entry.score,
                    entry.aggregate,
                    listings,
                    snapshot,
                    stats,
                    deadline,
                    failed_sources,
                )

    def _add_carried(
        self,
        score: int,
        carried: CarryOverCandidate,
        snapshot: SnapshotBuilder,
        stats: CrawlStats,
    ) -> None:
        record = snapshot.add_carried(carried.record)
        stats.carried_over += 1
        self._logger.info(
            "새 스냅샷 %s/%s (이어붙임, min_score=%s): %s/%s",
            snapshot.size,
            self._config.target_total_posts,
            score,
            record.get(SITE_FIELD),
            record.get(POST_ID_FIELD),
        )

    def _add_fresh(
        self,
        score: int,
        aggregate: CandidateAggregate,
        listings: ListingClient,
        snapshot: SnapshotBuilder,
        stats: CrawlStats,
        deadline: Deadline,
        failed_sources: frozenset[str],
    ) -> None:
        candidate = aggregate.candidate
        original_url = snapshot.cached_original_url(candidate.key)
        if original_url:
            stats.cached_url_hits += 1
            origin = "캐시"
        else:
            original_url = self._resolve_original_url(listings, candidate, stats)
            if original_url is None:
                return
            origin = "신규 조회"
            deadline.sleep(random.uniform(*REDIRECT_DELAY_SECONDS))

        snapshot.add(candidate, original_url, aggregate.source_scores, failed_sources)
        stats.saved += 1
        self._logger.info(
            "새 스냅샷 %s/%s (%s, min_score=%s): %s/%s",
            snapshot.size,
            self._config.target_total_posts,
            origin,
            score,
            candidate.site,
            candidate.post_id,
        )

    def _resolve_original_url(
        self,
        listings: ListingClient,
        candidate: ListingCandidate,
        stats: CrawlStats,
    ) -> str | None:
        redirect = listings.resolve_redirect(candidate.issue_link)
        stats.cupid_challenges += redirect.challenge_count
        stats.clearance_refreshes += redirect.clearance_refreshes
        stats.network_errors += redirect.network_errors
        if redirect.original_url is None:
            stats.request_failures += 1
            self._logger.warning("원문 주소 확보 실패, 건너뜀: %s", candidate.issue_link)
            return None
        stats.redirect_successes += 1
        return redirect.original_url

    def _collect_candidates(
        self,
        listings: ListingClient,
        stats: CrawlStats,
        deadline: Deadline,
        reference_time: datetime,
    ) -> CandidateCollection:
        candidates: dict[RecordKey, CandidateAggregate] = {}
        failed_sources: set[str] = set()
        discovery_order = 0

        for source, _ in SOURCE_LISTS:
            source_score = 0
            for page_number in range(1, self._config.max_pages + 1):
                deadline.ensure_available()
                page_started = time.perf_counter()
                try:
                    page = listings.read_page(
                        source,
                        page_number,
                        self._config.retention_hours,
                        reference_time,
                    )
                except PlaywrightError as error:
                    stats.listing_failures += 1
                    failed_sources.add(source)
                    self._logger.warning(
                        "목록 수집 중단, 남은 페이지 건너뜀: source=%s page=%s/%s "
                        "누적 고유=%s error=%s",
                        source,
                        page_number,
                        self._config.max_pages,
                        len(candidates),
                        type(error).__name__,
                    )
                    break
                stats.expired_skipped += page.expired_count
                stats.listed += len(page.candidates) + page.expired_count

                for candidate in page.candidates:
                    deadline.ensure_available()
                    source_score += 1
                    discovery_order += 1
                    aggregate = candidates.get(candidate.key)
                    if aggregate is None:
                        candidates[candidate.key] = CandidateAggregate(
                            candidate,
                            discovery_order,
                            {source: source_score},
                        )
                    else:
                        stats.duplicates += 1
                        aggregate.source_scores.setdefault(source, source_score)

                self._logger.info(
                    "목록 수집: source=%s page=%s/%s rows=%s 누적 고유=%s %.1f초",
                    source,
                    page_number,
                    self._config.max_pages,
                    len(page.candidates),
                    len(candidates),
                    time.perf_counter() - page_started,
                )
                if page_number < self._config.max_pages:
                    deadline.sleep(random.uniform(*LISTING_PAGE_DELAY_SECONDS))

            self._logger.info(
                "수집 종료: source=%s rows=%s unique_total=%s",
                source,
                source_score,
                len(candidates),
            )

        if not candidates:
            raise RuntimeError("IssueLink 목록에서 수집된 후보가 없습니다.")
        return CandidateCollection(candidates, frozenset(failed_sources))

    @staticmethod
    def _rank_entries(
        candidates: Mapping[RecordKey, CandidateAggregate],
        carried: list[CarryOverCandidate],
        snapshot: SnapshotBuilder,
        failed_sources: frozenset[str],
    ) -> list[RankedEntry]:
        """Rank fresh and carried posts together so a failed list keeps its share."""
        entries: list[RankedEntry] = []
        for aggregate in candidates.values():
            scores = list(aggregate.source_scores.values())
            if failed_sources:
                scores.extend(
                    score
                    for source, score in snapshot.cached_source_scores(
                        aggregate.candidate.key
                    ).items()
                    if source in failed_sources and source not in aggregate.source_scores
                )
            entries.append(
                RankedEntry(
                    score=min(scores),
                    order=(0, aggregate.discovery_order),
                    aggregate=aggregate,
                )
            )

        for index, carry in enumerate(carried):
            entries.append(RankedEntry(score=carry.score, order=(1, index), carried=carry))

        return sorted(entries, key=lambda entry: (entry.score, entry.order))

    def _log_summary(
        self,
        stats: CrawlStats,
        snapshot: SnapshotBuilder,
        elapsed: float,
    ) -> None:
        self._logger.info(
            "완료: %.1f초, 목록 %s개, 새 스냅샷 %s개, URL 캐시 재사용 %s개, 중복 %s개, "
            "만료 스킵 %s개, 리다이렉트 성공 %s개, CUPID 챌린지 %s회, "
            "쿠키 갱신 %s회, 네트워크 오류 %s회, 최종 실패(원문 주소 확보 실패) %s개, "
            "목록 로드 실패 %s회, 실패 수집처 %s, 이어붙임 %s개, JSON 총 %s개",
            elapsed,
            stats.listed,
            stats.saved,
            stats.cached_url_hits,
            stats.duplicates,
            stats.expired_skipped,
            stats.redirect_successes,
            stats.cupid_challenges,
            stats.clearance_refreshes,
            stats.network_errors,
            stats.request_failures,
            stats.listing_failures,
            list(stats.failed_sources),
            stats.carried_over,
            snapshot.size,
        )
        self._logger.info("저장 위치: %s", self._repository.path.resolve())

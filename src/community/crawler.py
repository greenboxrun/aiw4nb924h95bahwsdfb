"""Application service for the realtime IssueLink crawler."""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime

from .clients import IssueLinkListingClient
from .models import CrawlStats
from .parsing import KST
from .ports import ListingClient, ListingClientFactory, RecordRepository
from .record_policy import SnapshotBuilder
from .timing import Deadline


@dataclass(frozen=True, slots=True)
class CrawlerConfig:
    target_total_posts: int = 1000
    initial_pages: int = 10
    max_pages: int = 20
    retention_hours: int = 48
    max_runtime_seconds: float | None = None
    headed: bool = False

    def validate(self) -> None:
        if self.target_total_posts < 1:
            raise ValueError("max-new-posts는 1 이상이어야 합니다.")
        if self.initial_pages != 10:
            raise ValueError("initial-pages는 반드시 10이어야 합니다.")
        if self.max_pages < self.initial_pages:
            raise ValueError("max-pages는 initial-pages(10) 이상이어야 합니다.")
        if self.retention_hours < 0:
            raise ValueError("retention-hours는 0 이상이어야 합니다.")
        if self.max_runtime_seconds is not None and self.max_runtime_seconds <= 0:
            raise ValueError("max-runtime-seconds는 1 이상이어야 합니다.")


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
            self._crawl_pages(
                listings,
                snapshot,
                stats,
                deadline,
                reference_time,
            )

        self._repository.save(snapshot.records)

        elapsed = time.perf_counter() - started
        self._logger.info(
            "완료: %.1f초, 목록 %s개, 새 스냅샷 %s개, URL 캐시 재사용 %s개, 중복 %s개, "
            "만료 스킵 %s개, 리다이렉트 성공 %s개, CUPID 챌린지 %s회, "
            "쿠키 갱신 %s회, 네트워크 오류 %s회, 최종 실패(원문 주소 확보 실패) %s개, "
            "JSON 총 %s개",
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
            snapshot.size,
        )
        self._logger.info("저장 위치: %s", self._repository.path.resolve())
        return stats

    def _crawl_pages(
        self,
        listings: ListingClient,
        snapshot: SnapshotBuilder,
        stats: CrawlStats,
        deadline: Deadline,
        reference_time: datetime,
    ) -> None:
        source_score = 0
        for page_number in range(1, self._config.max_pages + 1):
            deadline.ensure_available()
            page_started = time.perf_counter()
            page = listings.read_page(
                page_number,
                self._config.retention_hours,
                reference_time,
            )
            stats.expired_skipped += page.expired_count
            stats.listed += len(page.candidates) + page.expired_count

            if not page.candidates and not page.expired_count:
                break

            for candidate in page.candidates:
                deadline.ensure_available()
                source_score += 1
                if snapshot.contains(candidate.key):
                    stats.duplicates += 1
                    continue

                original_url = snapshot.cached_original_url(candidate.key)
                if original_url:
                    stats.cached_url_hits += 1
                    source = "캐시"
                else:
                    redirect = listings.resolve_redirect(candidate.issue_link)
                    stats.cupid_challenges += redirect.challenge_count
                    stats.clearance_refreshes += redirect.clearance_refreshes
                    stats.network_errors += redirect.network_errors
                    if redirect.original_url is None:
                        stats.request_failures += 1
                        self._logger.warning(
                            "원문 주소 확보 실패, 건너뜀: %s",
                            candidate.issue_link,
                        )
                        continue
                    stats.redirect_successes += 1
                    original_url = redirect.original_url
                    source = "신규 조회"
                    deadline.sleep(random.uniform(0.5, 0.8))

                snapshot.add(candidate, original_url, source_score)
                stats.saved += 1
                self._logger.info(
                    "새 스냅샷 %s/%s (%s): %s/%s",
                    stats.saved,
                    self._config.target_total_posts,
                    source,
                    candidate.site,
                    candidate.post_id,
                )
                if snapshot.size >= self._config.target_total_posts:
                    break

            self._logger.info(
                "목록 %s/%s: %s개, 새 스냅샷 누적 %s개, %.1f초",
                page_number,
                self._config.max_pages,
                len(page.candidates),
                stats.saved,
                time.perf_counter() - page_started,
            )
            if snapshot.size >= self._config.target_total_posts:
                break
            deadline.sleep(random.uniform(0.7, 1.2))

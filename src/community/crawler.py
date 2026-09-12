"""Application service for the realtime IssueLink crawler."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from .clients import IssueLinkListingClient
from .models import CrawlStats, ListingCandidate
from .parsing import KST
from .ports import ListingClient, ListingClientFactory, RecordRepository
from .record_policy import SnapshotBuilder
from .settings import PAGES_PER_LIST, SOURCE_LISTS, TITLE_EXCLUDE_KEYWORDS
from .timing import Deadline


@dataclass(frozen=True, slots=True)
class CrawlerConfig:
    target_total_posts: int = 1000
    initial_pages: int = 10
    max_pages: int = PAGES_PER_LIST
    retention_hours: int = 48
    max_runtime_seconds: float | None = None
    headed: bool = False

    def validate(self) -> None:
        if self.target_total_posts < 1:
            raise ValueError("max-new-posts는 1 이상이어야 합니다.")
        if self.initial_pages != 10:
            raise ValueError("initial-pages는 반드시 10이어야 합니다.")
        if self.max_pages != PAGES_PER_LIST:
            raise ValueError(f"max-pages는 {PAGES_PER_LIST}이어야 합니다.")
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
        candidates = self._collect_candidates(
            listings,
            stats,
            deadline,
            reference_time,
        )
        ranked_candidates = self._rank_candidates(candidates)
        self._logger.info(
            "후보 병합 완료: 고유 %s개, 제목 필터 제외 %s개, 정렬 후보 %s개",
            len(candidates),
            len(candidates) - len(ranked_candidates),
            len(ranked_candidates),
        )

        for aggregate in ranked_candidates:
            deadline.ensure_available()
            if snapshot.size >= self._config.target_total_posts:
                break

            candidate = aggregate.get("candidate")
            source_scores = aggregate.get("source_scores")
            if not isinstance(candidate, ListingCandidate) or not isinstance(
                source_scores, dict
            ) or not source_scores:
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

            snapshot.add(candidate, original_url, source_scores)
            stats.saved += 1
            self._logger.info(
                "새 스냅샷 %s/%s (%s, min_score=%s): %s/%s",
                stats.saved,
                self._config.target_total_posts,
                source,
                min(source_scores.values()),
                candidate.site,
                candidate.post_id,
            )

    def _collect_candidates(
        self,
        listings: ListingClient,
        stats: CrawlStats,
        deadline: Deadline,
        reference_time: datetime,
    ) -> dict[tuple[str, str], dict[str, object]]:
        candidates: dict[tuple[str, str], dict[str, object]] = {}
        discovery_order = 0

        for source, _ in SOURCE_LISTS:
            source_score = 0
            for page_number in range(1, self._config.max_pages + 1):
                deadline.ensure_available()
                page_started = time.perf_counter()
                page = listings.read_page(
                    source,
                    page_number,
                    self._config.retention_hours,
                    reference_time,
                )
                stats.expired_skipped += page.expired_count
                stats.listed += len(page.candidates) + page.expired_count

                for candidate in page.candidates:
                    deadline.ensure_available()
                    source_score += 1
                    discovery_order += 1
                    aggregate = candidates.get(candidate.key)
                    if aggregate is None:
                        candidates[candidate.key] = {
                            "candidate": candidate,
                            "source_scores": {source: source_score},
                            "discovery_order": discovery_order,
                        }
                    else:
                        stats.duplicates += 1
                        scores = aggregate.get("source_scores")
                        if isinstance(scores, dict):
                            scores.setdefault(source, source_score)

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
                    deadline.sleep(random.uniform(0.8, 1.6))

            self._logger.info(
                "수집 종료: source=%s rows=%s unique_total=%s",
                source,
                source_score,
                len(candidates),
            )

        return candidates

    @staticmethod
    def _rank_candidates(
        candidates: Mapping[tuple[str, str], dict[str, object]],
    ) -> list[dict[str, object]]:
        filtered: list[dict[str, object]] = []
        for aggregate in candidates.values():
            candidate = aggregate.get("candidate")
            if not isinstance(candidate, ListingCandidate):
                continue
            if any(keyword in candidate.title for keyword in TITLE_EXCLUDE_KEYWORDS):
                continue
            source_scores = aggregate.get("source_scores")
            if not isinstance(source_scores, dict) or not source_scores:
                continue
            filtered.append(aggregate)

        return sorted(
            filtered,
            key=lambda aggregate: (
                min(aggregate["source_scores"].values()),
                aggregate["discovery_order"],
            ),
        )

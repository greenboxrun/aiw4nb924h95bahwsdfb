"""Application service for the realtime IssueLink crawler."""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Any

from .clients import IssueLinkListingClient
from .models import CrawlStats, ListingCandidate
from .parsing import record_key, remove_expired
from .repository import JsonRecordRepository
from .timing import CrawlDeadlineExceeded, Deadline


@dataclass(frozen=True, slots=True)
class CrawlerConfig:
    max_new_posts: int = 1000
    max_pages: int = 100
    retention_hours: int = 48
    max_runtime_seconds: int = 270
    headed: bool = False

    def validate(self) -> None:
        if self.max_new_posts < 1 or self.max_pages < 1:
            raise ValueError("max-new-posts와 max-pages는 1 이상이어야 합니다.")
        if self.retention_hours < 0:
            raise ValueError("retention-hours는 0 이상이어야 합니다.")
        if self.max_runtime_seconds <= 0:
            raise ValueError("max-runtime-seconds는 1 이상이어야 합니다.")


class RealtimeCrawler:
    """Coordinate listing, redirect resolution, deduplication, and persistence."""

    def __init__(
        self,
        repository: JsonRecordRepository,
        config: CrawlerConfig,
        logger: logging.Logger,
    ) -> None:
        config.validate()
        self._repository = repository
        self._config = config
        self._logger = logger

    def run(self) -> CrawlStats:
        started = time.perf_counter()
        deadline = Deadline(self._config.max_runtime_seconds)
        records = self._repository.load()
        retained = remove_expired(records, self._config.retention_hours)
        removed = len(records) - len(retained)
        self._repository.save(retained)
        self._logger.info("기존 데이터: %s개, 만료 삭제: %s개", len(records), removed)

        stats = CrawlStats()
        known_keys = {
            key for record in retained if (key := record_key(record)) is not None
        }
        record_indexes = {
            key: index
            for index, record in enumerate(retained)
            if (key := record_key(record)) is not None
        }

        try:
            with IssueLinkListingClient(
                deadline,
                self._logger,
                headed=self._config.headed,
            ) as listings:
                self._crawl_pages(
                    listings,
                    retained,
                    known_keys,
                    record_indexes,
                    stats,
                    deadline,
                )
        except CrawlDeadlineExceeded:
            self._logger.warning(
                "%s초 실행 제한에 도달해 현재까지 저장된 결과로 중단했습니다.",
                self._config.max_runtime_seconds,
            )

        elapsed = time.perf_counter() - started
        self._logger.info(
            "완료: %.1f초, 목록 %s개, 신규 %s개, 갱신 %s개, 중복 %s개, "
            "만료 스킵 %s개, 리다이렉트 성공 %s개, CUPID 챌린지 %s회, "
            "쿠키 갱신 %s회, 네트워크 오류 %s회, 최종 실패(원문 주소 확보 실패) %s개, "
            "JSON 총 %s개",
            elapsed,
            stats.listed,
            stats.saved,
            stats.updated,
            stats.duplicates,
            stats.expired_skipped,
            stats.redirect_successes,
            stats.cupid_challenges,
            stats.clearance_refreshes,
            stats.network_errors,
            stats.request_failures,
            len(retained),
        )
        self._logger.info("저장 위치: %s", self._repository.path.resolve())
        return stats

    def _crawl_pages(
        self,
        listings: IssueLinkListingClient,
        records: list[dict[str, Any]],
        known_keys: set[tuple[str, str]],
        record_indexes: dict[tuple[str, str], int],
        stats: CrawlStats,
        deadline: Deadline,
    ) -> None:
        for page_number in range(1, self._config.max_pages + 1):
            deadline.ensure_available()
            page_started = time.perf_counter()
            page_changed = False
            page = listings.read_page(page_number, self._config.retention_hours)
            stats.expired_skipped += page.expired_count
            stats.listed += len(page.candidates) + page.expired_count

            if not page.candidates and not page.expired_count:
                break

            try:
                for candidate in page.candidates:
                    deadline.ensure_available()
                    if candidate.key in known_keys:
                        stats.duplicates += 1
                        existing = records[record_indexes[candidate.key]]
                        if self._update_existing(existing, candidate):
                            stats.updated += 1
                            page_changed = True
                        continue

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
                    records.append(candidate.to_record(redirect.original_url))
                    known_keys.add(candidate.key)
                    record_indexes[candidate.key] = len(records) - 1
                    stats.saved += 1
                    page_changed = True
                    self._logger.info(
                        "저장 %s/%s: %s/%s",
                        stats.saved,
                        self._config.max_new_posts,
                        candidate.site,
                        candidate.post_id,
                    )
                    if stats.saved >= self._config.max_new_posts:
                        break
                    deadline.sleep(random.uniform(0.5, 0.8))
            finally:
                if page_changed:
                    self._repository.save(records)

            self._logger.info(
                "목록 %s/%s: %s개, 신규 누적 %s개, %.1f초",
                page_number,
                self._config.max_pages,
                len(page.candidates),
                stats.saved,
                time.perf_counter() - page_started,
            )
            if stats.saved >= self._config.max_new_posts:
                break
            deadline.sleep(random.uniform(0.7, 1.2))

    @staticmethod
    def _update_existing(record: dict[str, Any], candidate: ListingCandidate) -> bool:
        changed = False
        values = {
            "제목": candidate.title,
            "작성시간": candidate.written_at,
            "댓글수": candidate.comment_count,
            "조회수": candidate.view_count,
        }
        for field, value in values.items():
            if record.get(field) != value:
                record[field] = value
                changed = True
        return changed

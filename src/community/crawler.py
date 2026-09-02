"""Application service for the realtime IssueLink crawler."""

from __future__ import annotations

import logging
import os
import random
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .clients import IssueLinkListingClient
from .models import CrawlStats, ListingCandidate
from .parsing import parse_integer, parse_written_at, record_key, remove_expired
from .repository import JsonRecordRepository
from .timing import CrawlDeadlineExceeded, Deadline


@dataclass(frozen=True, slots=True)
class CrawlerConfig:
    target_total_posts: int = 1000
    initial_pages: int = 10
    max_pages: int = 15
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
        retained = self._deduplicate_records(
            remove_expired(records, self._config.retention_hours)
        )
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
            if self._config.max_runtime_seconds is not None:
                self._logger.warning(
                    "%s초 실행 제한에 도달해 현재까지 저장된 결과로 중단했습니다.",
                    self._config.max_runtime_seconds,
                )
        finally:
            self._limit_to_target(records=retained)
            self._repository.save(retained)
            self._push_checkpoint(retained, stats.saved)

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
        self._crawl_page_range(
            listings,
            records,
            known_keys,
            record_indexes,
            stats,
            deadline,
            start_page=1,
            end_page=self._config.initial_pages,
            stop_at_target=False,
        )

        self._limit_to_target(records=records)
        known_keys, record_indexes = self._build_record_indexes(records)
        self._repository.save(records)
        self._logger.info(
            "필수 목록 1~%s페이지 처리 후 JSON %s개",
            self._config.initial_pages,
            len(records),
        )

        if len(records) >= self._config.target_total_posts:
            return

        self._crawl_page_range(
            listings,
            records,
            known_keys,
            record_indexes,
            stats,
            deadline,
            start_page=self._config.initial_pages + 1,
            end_page=self._config.max_pages,
            stop_at_target=True,
        )

    def _crawl_page_range(
        self,
        listings: IssueLinkListingClient,
        records: list[dict[str, Any]],
        known_keys: set[tuple[str, str]],
        record_indexes: dict[tuple[str, str], int],
        stats: CrawlStats,
        deadline: Deadline,
        *,
        start_page: int,
        end_page: int,
        stop_at_target: bool,
    ) -> None:
        for page_number in range(start_page, end_page + 1):
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

                    if stop_at_target and len(records) >= self._config.target_total_posts:
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
                        self._config.target_total_posts,
                        candidate.site,
                        candidate.post_id,
                    )
                    deadline.sleep(random.uniform(0.5, 0.8))
            finally:
                if page_changed:
                    self._repository.save(records)

            self._logger.info(
                "목록 %s/%s: %s개, 신규 누적 %s개, %.1f초",
                page_number,
                end_page,
                len(page.candidates),
                stats.saved,
                time.perf_counter() - page_started,
            )
            if stop_at_target and len(records) >= self._config.target_total_posts:
                break
            deadline.sleep(random.uniform(0.7, 1.2))

    def _limit_to_target(self, *, records: list[dict[str, Any]]) -> None:
        """Keep the highest-view records, with newer posts winning view-count ties."""
        records.sort(key=self._record_sort_key)
        del records[self._config.target_total_posts :]

    @staticmethod
    def _record_sort_key(record: dict[str, Any]) -> tuple[int, int, str, str]:
        written_at = parse_written_at(str(record.get("작성시간", "")))
        written_sort_value = RealtimeCrawler._datetime_sort_value(written_at)
        return (
            -parse_integer(str(record.get("조회수", ""))),
            -written_sort_value,
            str(record.get("사이트", "")),
            str(record.get("id값", "")),
        )

    @staticmethod
    def _datetime_sort_value(value: datetime | None) -> int:
        if value is None:
            return 0
        return (
            value.toordinal() * 86_400
            + value.hour * 3_600
            + value.minute * 60
            + value.second
        )

    @staticmethod
    def _deduplicate_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep the last stored copy for each source-site/post-ID pair."""
        unique_records: list[dict[str, Any]] = []
        record_indexes: dict[tuple[str, str], int] = {}
        for record in records:
            key = record_key(record)
            if key is None:
                unique_records.append(record)
                continue
            if key in record_indexes:
                unique_records[record_indexes[key]] = record
                continue
            record_indexes[key] = len(unique_records)
            unique_records.append(record)
        return unique_records

    @staticmethod
    def _build_record_indexes(
        records: list[dict[str, Any]],
    ) -> tuple[set[tuple[str, str]], dict[tuple[str, str], int]]:
        indexes = {
            key: index
            for index, record in enumerate(records)
            if (key := record_key(record)) is not None
        }
        return set(indexes), indexes

    def _push_checkpoint(self, records: list[dict[str, Any]], saved_count: int) -> None:
        """Push a saved checkpoint when running inside GitHub Actions."""
        if os.environ.get("GITHUB_ACTIONS") != "true":
            return

        repository_path = self._repository.path.resolve()
        project_root = Path(__file__).resolve().parents[2]
        try:
            relative_path = repository_path.relative_to(project_root)
        except ValueError as error:
            raise RuntimeError(
                f"checkpoint 대상 파일이 저장소 밖에 있습니다: {repository_path}"
            ) from error

        def run_git(*arguments: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["git", *arguments],
                cwd=project_root,
                check=True,
                capture_output=True,
                text=True,
            )

        run_git("add", str(relative_path))
        staged = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=project_root,
            capture_output=True,
            text=True,
        )
        if staged.returncode == 0:
            return
        if staged.returncode != 1:
            raise RuntimeError(staged.stderr.strip() or "Git staged diff 확인 실패")

        run_git("config", "user.name", "github-actions[bot]")
        run_git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
        run_git("commit", "-m", "Checkpoint community crawl result")
        last_error = ""
        for attempt in range(3):
            try:
                run_git("push")
                self._logger.info(
                    "checkpoint push 완료: 신규 %s개, 전체 %s개",
                    saved_count,
                    len(records),
                )
                return
            except subprocess.CalledProcessError as error:
                last_error = error.stderr.strip() or str(error)
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"checkpoint push 실패: {last_error}")

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

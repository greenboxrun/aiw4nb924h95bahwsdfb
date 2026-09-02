"""CLI entry point for the production IssueLink realtime crawler."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from src.common.paths import RESULT_ROOT

from .crawler import CrawlerConfig, RealtimeCrawler
from .repository import JsonRecordRepository


LOGGER = logging.getLogger("community.issuelink_realtime_crawler")
DEFAULT_OUTPUT = RESULT_ROOT / "community" / "result.json"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="IssueLink 실시간 커뮤니티 JSON 누적 크롤러")
    parser.add_argument(
        "--max-new-posts",
        dest="target_total_posts",
        type=int,
        default=1000,
        help="최종 보관 최대 게시물 수",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=15,
        help="최대 목록 페이지 수(필수 10페이지 포함, 10 이상)",
    )
    parser.add_argument("--retention-hours", type=int, default=48)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--headed", action="store_true", help="브라우저 창 표시")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING"),
    )
    return parser.parse_args(argv)


def configure_logging(level: str) -> None:
    numeric_level = getattr(logging, level.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError(f"지원하지 않는 로그 레벨입니다: {level}")
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.log_level)
    try:
        crawler = RealtimeCrawler(
            JsonRecordRepository(args.output),
            CrawlerConfig(
                target_total_posts=args.target_total_posts,
                max_pages=args.max_pages,
                retention_hours=args.retention_hours,
                headed=args.headed,
            ),
            LOGGER,
        )
        crawler.run()
    except Exception as error:
        LOGGER.error("커뮤니티 크롤링 실패: %s", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

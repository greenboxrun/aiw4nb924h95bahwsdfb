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
    parser.add_argument("--max-new-posts", type=int, default=1000)
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--retention-hours", type=int, default=48)
    parser.add_argument("--max-runtime-seconds", type=int, default=270)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--headed", action="store_true", help="브라우저 창 표시")
    return parser.parse_args(argv)


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = parse_args(argv)
    try:
        crawler = RealtimeCrawler(
            JsonRecordRepository(args.output),
            CrawlerConfig(
                max_new_posts=args.max_new_posts,
                max_pages=args.max_pages,
                retention_hours=args.retention_hours,
                max_runtime_seconds=args.max_runtime_seconds,
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

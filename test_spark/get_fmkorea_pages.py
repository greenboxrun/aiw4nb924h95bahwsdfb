"""이슈링크 펨코 목록을 여러 페이지 수집하는 스파크."""

from __future__ import annotations

import argparse
import random
import time
from datetime import datetime
from pathlib import Path

import requests

from get_fmkorea_list import Post, parse_posts, write_csv


BASE_URL = (
    "https://www.issuelink.co.kr/community/listview/fmkorea/72/"
    "comment/_self/blank/blank/blank"
)
DEFAULT_MIN_DELAY = 4.0
DEFAULT_MAX_DELAY = 8.0


def fetch_pages(
    page_count: int = 6,
    min_delay: float = DEFAULT_MIN_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
) -> list[Post]:
    """1페이지부터 page_count페이지까지 순서대로 수집한다."""
    if not 1 <= page_count <= 6:
        raise ValueError("페이지 수는 1~6 사이여야 합니다.")
    if min_delay < 0 or max_delay < min_delay:
        raise ValueError("대기 시간 범위가 올바르지 않습니다.")

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    posts: list[Post] = []

    for page in range(1, page_count + 1):
        url = f"{BASE_URL}/{page}"
        response = session.get(url, timeout=20)
        response.raise_for_status()
        page_posts = parse_posts(response.text)
        posts.extend(page_posts)
        print(f"{page}/{page_count}페이지 수집 완료: {len(page_posts)}개")

        if page < page_count:
            delay = random.uniform(min_delay, max_delay)
            print(f"다음 요청까지 {delay:.1f}초 대기합니다.")
            time.sleep(delay)

    return posts


def main() -> int:
    parser = argparse.ArgumentParser(description="펨코 목록 최대 6페이지 수집")
    parser.add_argument("-p", "--pages", type=int, default=6, help="수집할 페이지 수(기본값: 6)")
    parser.add_argument(
        "--min-delay",
        type=float,
        default=DEFAULT_MIN_DELAY,
        help="페이지 사이 최소 대기 초(기본값: 4)",
    )
    parser.add_argument(
        "--max-delay",
        type=float,
        default=DEFAULT_MAX_DELAY,
        help="페이지 사이 최대 대기 초(기본값: 8)",
    )
    parser.add_argument("-o", "--output", type=Path, help="CSV 저장 경로")
    args = parser.parse_args()

    posts = fetch_pages(args.pages, args.min_delay, args.max_delay)
    output = args.output or (
        Path(__file__).resolve().parent
        / "log"
        / f"{datetime.now():%Y%m%d%H%M%S}.csv"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    write_csv(posts, output)
    print(f"총 {len(posts)}개 게시글을 저장했습니다: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

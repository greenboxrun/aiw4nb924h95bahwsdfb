"""이슈링크에서 펨코 게시글 목록을 가져오는 스파크."""

from __future__ import annotations

import argparse
import csv
import re
import sys
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path

import requests
from bs4 import BeautifulSoup


URL = "https://www.issuelink.co.kr/community/listview/all/72/comment/_self/blank/blank/blank"
COMMENT_COUNT_RE = re.compile(r"\s*\[(\d+)\]\s*$")


@dataclass(frozen=True)
class Post:
    time: str
    title: str
    comments: int


def parse_posts(html: str) -> list[Post]:
    """목록 HTML에서 펨코 게시글을 추출한다."""
    soup = BeautifulSoup(html, "html.parser")
    posts: list[Post] = []

    for row in soup.select("table tbody tr"):
        source = row.select_one("td.rank_bg small")
        title_element = row.select_one(".title")
        date_element = row.select_one(".second_date span")
        if not source or source.get_text(strip=True) != "펨코":
            continue
        if not title_element or not date_element:
            continue

        title_text = title_element.get_text(" ", strip=True)
        match = COMMENT_COUNT_RE.search(title_text)
        if match is None:
            # 댓글 수가 없는 비정상 행은 결과를 망가뜨리지 않도록 건너뛴다.
            continue

        posts.append(
            Post(
                time=date_element.get_text(strip=True),
                title=title_text[: match.start()].strip(),
                comments=int(match.group(1)),
            )
        )

    return posts


def fetch_posts(url: str = URL) -> list[Post]:
    """지정한 목록 페이지에서 펨코 게시글을 수집한다."""
    response = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=20,
    )
    response.raise_for_status()
    return parse_posts(response.text)


def write_csv(posts: list[Post], output: Path) -> None:
    with output.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        writer.writerow(["시간", "제목", "댓글수"])
        writer.writerows((post.time, post.title, post.comments) for post in posts)


def main() -> int:
    parser = argparse.ArgumentParser(description="펨코 게시글 목록 수집")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="CSV 저장 경로 (기본값: test_spark/log/yyyymmddhhmmss.csv)",
    )
    args = parser.parse_args()

    try:
        posts = fetch_posts()
    except Exception as error:
        print(f"목록을 가져오지 못했습니다: {error}", file=sys.stderr)
        return 1

    output = args.output
    if output is None:
        output = (
            Path(__file__).resolve().parent
            / "log"
            / f"{datetime.now():%Y%m%d%H%M%S}.csv"
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    write_csv(posts, output)
    print(f"{len(posts)}개 게시글을 저장했습니다: {output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

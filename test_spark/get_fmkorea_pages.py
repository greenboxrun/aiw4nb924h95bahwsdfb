"""이슈링크 펨코 목록을 여러 페이지 수집하는 스파크."""

from __future__ import annotations

import argparse
import json
import random
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from get_fmkorea_list import COMMENT_COUNT_RE, Post, parse_posts


BASE_URL = (
    "https://www.issuelink.co.kr/community/listview/fmkorea/72/"
    "comment/_self/blank/blank/blank"
)
DEFAULT_MIN_DELAY = 4.0
DEFAULT_MAX_DELAY = 8.0
BLOCK_MARKERS = (
    "captcha",
    "cloudflare",
    "access denied",
    "blocked",
    "too many requests",
    "just a moment",
)


def log_page_diagnostics(
    *,
    url: str,
    response: requests.Response,
    page_posts: list[Post],
) -> None:
    """페이지 응답과 파싱 결과를 원인 분석용으로 상세히 출력한다."""
    soup = BeautifulSoup(response.text, "html.parser")
    rows = soup.select("table tbody tr")
    source_rows = soup.select("table tbody tr td.rank_bg.s_bg_fmkorea")
    source_texts = [
        cell.get_text(" ", strip=True)
        for cell in soup.select("table tbody tr td.rank_bg small")
    ]
    comment_format_failures = 0
    for row in rows:
        title_element = row.select_one(".title")
        if title_element and COMMENT_COUNT_RE.search(
            title_element.get_text(" ", strip=True)
        ) is None:
            comment_format_failures += 1

    response_lower = response.text.lower()
    detected_markers = [
        marker for marker in BLOCK_MARKERS if marker in response_lower
    ]
    title = soup.title.get_text(" ", strip=True) if soup.title else ""

    print(f"[진단] 요청 URL: {url}")
    print(f"[진단] HTTP 상태: {response.status_code}")
    print(f"[진단] 최종 URL: {response.url}")
    print(f"[진단] Content-Type: {response.headers.get('Content-Type', '')}")
    print(
        "[진단] 인코딩: "
        f"response={response.encoding!r}, apparent={response.apparent_encoding!r}"
    )
    print(
        "[진단] 응답 크기: "
        f"{len(response.content):,} bytes / {len(response.text):,} chars"
    )
    print(f"[진단] HTML 제목: {title!r}")
    print(
        "[진단] HTML 구조: "
        f"table={len(soup.select('table'))}, tr={len(rows)}"
    )
    print(
        "[진단] 게시판 식별: "
        f"class=s_bg_fmkorea {len(source_rows)}개, 텍스트 '펨코' "
        f"{response.text.count('펨코')}회"
    )
    print(f"[진단] 파싱 결과: {len(page_posts)}개")
    print(f"[진단] 댓글 수 형식 불일치: {comment_format_failures}개")
    print(f"[진단] 차단 의심 키워드: {detected_markers or '없음'}")

    if source_texts:
        print(f"[진단] 발견된 게시판 텍스트 샘플: {source_texts[:10]!r}")

    if not page_posts:
        snippet = " ".join(response.text[:800].split())
        print(f"[진단] 0개 응답 앞부분(최대 800자): {snippet!r}")


def write_json(
    posts: list[Post],
    output: Path,
    *,
    page_count: int,
) -> None:
    """수집 결과와 재사용에 도움이 되는 메타정보를 JSON으로 저장한다."""
    saved_at = datetime.now().astimezone()
    payload = {
        "schema_version": 1,
        "source_url": BASE_URL,
        "pages_requested": page_count,
        "post_count": len(posts),
        "created_at": saved_at.isoformat(timespec="seconds"),
        "created_at_utc": saved_at.astimezone(timezone.utc).isoformat(
            timespec="seconds"
        ),
        "posts": [asdict(post) for post in posts],
    }

    with output.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
        file.write("\n")


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
        log_page_diagnostics(
            url=url,
            response=response,
            page_posts=page_posts,
        )
        if not page_posts:
            raise RuntimeError(
                f"{page}페이지에서 게시글을 찾지 못했습니다. "
                "위의 [진단] 로그를 확인하세요."
            )
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
    parser.add_argument("-o", "--output", type=Path, help="JSON 저장 경로")
    args = parser.parse_args()

    posts = fetch_pages(args.pages, args.min_delay, args.max_delay)
    output = args.output or (
        Path(__file__).resolve().parent.parent
        / "result"
        / "result.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = output.with_name(f".{output.name}.tmp")
    try:
        write_json(
            posts,
            temporary_output,
            page_count=args.pages,
        )
        temporary_output.replace(output)
    finally:
        temporary_output.unlink(missing_ok=True)
    print(f"총 {len(posts)}개 게시글을 저장했습니다: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

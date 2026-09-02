"""이슈링크 펨코 목록을 여러 페이지 수집하는 운영 크롤러."""

from __future__ import annotations

import argparse
import json
import random
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from src.common.paths import RESULT_ROOT
from src.common.files import atomic_write_text

from .get_fmkorea_list import COMMENT_COUNT_RE, Post, parse_posts


BASE_URL = (
    "https://www.issuelink.co.kr/community/listview/fmkorea/72/"
    "comment/_self/blank/blank/blank"
)
DEFAULT_MIN_DELAY = 4.0
DEFAULT_MAX_DELAY = 8.0
POST_ID_RE = re.compile(r"/community/go/fmkorea/(\d+)(?:$|[/?#])")
BLOCK_MARKERS = (
    "captcha",
    "cloudflare",
    "access denied",
    "blocked",
    "too many requests",
    "just a moment",
)


@dataclass(frozen=True)
class CrawledPost(Post):
    """펨코 게시물 목록에서 수집한 게시물 정보."""

    url: str
    postid: str


def parse_page_posts(html: str) -> list[CrawledPost]:
    """게시물 목록에서 기존 정보와 게시물 URL/postid를 함께 추출한다."""
    soup = BeautifulSoup(html, "html.parser")
    parsed_posts = parse_posts(html)
    posts: list[CrawledPost] = []
    candidate_rows = []
    for row in soup.select("table tbody tr"):
        source = row.select_one("td.rank_bg small")
        title_element = row.select_one(".title")
        date_element = row.select_one(".second_date span")
        if not source or source.get_text(strip=True) != "펨코":
            continue
        if not title_element or not date_element:
            continue
        if COMMENT_COUNT_RE.search(title_element.get_text(" ", strip=True)) is None:
            continue
        candidate_rows.append(row)

    for row, post in zip(
        candidate_rows,
        parsed_posts,
    ):
        title_link = row.select_one(".title a[href]")
        if title_link is None:
            continue

        source_url = title_link.get("href", "").strip()
        match = POST_ID_RE.search(source_url)
        if match is None:
            continue
        postid = match.group(1)
        url = f"https://www.fmkorea.com/{postid}"

        posts.append(
            CrawledPost(
                time=post.time,
                title=post.title,
                comments=post.comments,
                url=url,
                postid=postid,
            )
        )

    return posts


def log_page_diagnostics(*, url: str, driver: webdriver.Chrome, page_posts: list[Post]) -> None:
    """페이지 응답과 파싱 결과를 원인 분석용으로 상세히 출력한다."""
    html = driver.page_source
    soup = BeautifulSoup(html, "html.parser")
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

    response_lower = html.lower()
    detected_markers = [
        marker for marker in BLOCK_MARKERS if marker in response_lower
    ]
    title = soup.title.get_text(" ", strip=True) if soup.title else ""

    print(f"[진단] 요청 URL: {url}")
    print("[진단] HTTP 상태: Selenium 브라우저 탐색으로 직접 확인 불가")
    print(f"[진단] 최종 URL: {driver.current_url}")
    print("[진단] Content-Type: Selenium 브라우저 탐색으로 직접 확인 불가")
    print(f"[진단] 인코딩: document.characterSet={driver.execute_script('return document.characterSet')!r}")
    print(
        "[진단] 응답 크기: "
        f"{len(html.encode('utf-8')):,} bytes / {len(html):,} chars"
    )
    print(f"[진단] HTML 제목: {title!r}")
    print(
        "[진단] HTML 구조: "
        f"table={len(soup.select('table'))}, tr={len(rows)}"
    )
    print(
        "[진단] 게시판 식별: "
        f"class=s_bg_fmkorea {len(source_rows)}개, 텍스트 '펨코' "
        f"{html.count('펨코')}회"
    )
    print(f"[진단] 파싱 결과: {len(page_posts)}개")
    print(f"[진단] 댓글 수 형식 불일치: {comment_format_failures}개")
    print(f"[진단] 차단 의심 키워드: {detected_markers or '없음'}")

    if source_texts:
        print(f"[진단] 발견된 게시판 텍스트 샘플: {source_texts[:10]!r}")

    if not page_posts:
        snippet = " ".join(html[:800].split())
        print(f"[진단] 0개 응답 앞부분(최대 800자): {snippet!r}")


def create_driver() -> webdriver.Chrome:
    """GitHub Actions와 로컬에서 모두 동작하는 헤드리스 Chrome을 만든다."""
    options = Options()
    options.page_load_strategy = "eager"
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--lang=ko-KR")
    options.add_argument("--user-agent=Mozilla/5.0")
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(45)
    return driver


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

    atomic_write_text(
        output,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )


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

    driver = create_driver()
    posts: list[Post] = []

    try:
        for page in range(1, page_count + 1):
            url = f"{BASE_URL}/{page}"
            print(f"[브라우저] {page}페이지 접속: {url}")
            try:
                try:
                    driver.get(url)
                except TimeoutException:
                    print(
                        "[브라우저] 페이지 로딩 타임아웃이 발생했지만 "
                        "게시글 DOM이 나타나는지 계속 확인합니다."
                    )
                WebDriverWait(driver, 35).until(
                    lambda current_driver: current_driver.find_elements(
                        By.CSS_SELECTOR, "td.rank_bg.s_bg_fmkorea"
                    )
                )
            except TimeoutException as error:
                log_page_diagnostics(url=url, driver=driver, page_posts=[])
                raise RuntimeError(
                    f"{page}페이지에서 브라우저 인증 또는 게시글 로딩이 완료되지 않았습니다."
                ) from error

            page_posts = parse_page_posts(driver.page_source)
            log_page_diagnostics(
                url=url,
                driver=driver,
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
    finally:
        driver.quit()

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
    try:
        posts = fetch_pages(args.pages, args.min_delay, args.max_delay)
    except Exception as error:
        print(f"FMKorea list crawl failed: {error}")
        return 1
    output = args.output or (RESULT_ROOT / "topic" / "result.json")
    write_json(posts, output, page_count=args.pages)
    print(f"총 {len(posts)}개 게시글을 저장했습니다: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

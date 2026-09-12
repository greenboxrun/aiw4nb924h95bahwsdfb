"""IssueLink 4개 목록을 로컬에서 수집해 JSON으로 저장하는 Spark 코드.

각 목록은 고정된 10페이지를 순서대로 읽는다. IssueLink의 목록 행에서
게시글 메타데이터만 파싱하며, ``community/go`` 링크를 원문 사이트로
리디렉션하거나 원문 사이트에 요청하지 않는다.

같은 ``사이트 + id값`` 게시글은 하나의 레코드로 병합하고, 발견된 목록마다
별도의 수집처와 목록별 순위를 저장한다.

결과 예시:

    {
      "사이트": "inven",
      "id값": "2725873",
      "제목": "게시글 제목",
      "작성시간": "2026-09-12 12:00:00",
      "댓글수": 10,
      "조회수": 1234,
      "원문URL": "",
      "수집처": [
        {"source": "issuelink_adj", "score": 12},
        {"source": "issuelink_read", "score": 4}
      ]
    }

설치:
    python -m pip install playwright
    python -m playwright install chromium

실행:
    python test_spark/20260912_1948_issuelink_multi_list_crawler.py
    python test_spark/20260912_1948_issuelink_multi_list_crawler.py --headed
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


SPARK_DIR = Path(__file__).resolve().parent
RESULT_DIR = SPARK_DIR / "result" / "issuelink_multi_list"
LOG_DIR = SPARK_DIR / "log"
PAGES_PER_LIST = 10
SOURCE_LISTS = (
    (
        "issuelink_adj",
        "https://www.issuelink.co.kr/community/listview/all/48/adj/_self/blank/blank/blank",
    ),
    (
        "issuelink_read",
        "https://www.issuelink.co.kr/community/listview/all/48/read/_self/blank/blank/blank",
    ),
    (
        "issuelink_comment",
        "https://www.issuelink.co.kr/community/listview/all/48/comment/_self/blank/blank/blank",
    ),
    (
        "issuelink_click",
        "https://www.issuelink.co.kr/community/listview/all/48/click/_self/blank/blank/blank",
    ),
)
BLOCKED_RESOURCE_TYPES = {"font", "image", "media", "stylesheet"}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
POST_LINK_SELECTOR = "table tr a[href*='/community/go/']"
LOGGER = logging.getLogger("issuelink_multi_list_crawler")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="IssueLink 4개 목록을 각각 10페이지씩 수집하는 로컬 Spark 코드"
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="결과 파일 또는 결과 디렉터리. 기본값은 test_spark/result/issuelink_multi_list",
    )
    parser.add_argument("--headed", action="store_true", help="브라우저 창 표시")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING"),
        help="로그 출력 수준",
    )
    return parser.parse_args(argv)


def configure_logging(log_path: Path, level: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    numeric_level = getattr(logging, level.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError(f"지원하지 않는 로그 레벨입니다: {level}")

    LOGGER.setLevel(numeric_level)
    LOGGER.propagate = False
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    file_handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    console_handler = logging.StreamHandler(sys.stdout)
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)
    LOGGER.handlers.clear()
    LOGGER.addHandler(file_handler)
    LOGGER.addHandler(console_handler)
    LOGGER.info("로그 파일: %s", log_path.resolve())


def timestamped_path(
    directory_or_file: Path | None,
    default_directory: Path,
    timestamp: str,
    stem: str,
    suffix: str,
) -> Path:
    """타임스탬프 파일을 만들고 기존 파일과의 충돌을 피한다."""
    if directory_or_file is None:
        directory = default_directory
        filename_stem = stem
    elif directory_or_file.suffix.lower() == suffix:
        directory = directory_or_file.parent
        filename_stem = directory_or_file.stem
    else:
        directory = directory_or_file
        filename_stem = stem

    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / f"{timestamp}_{filename_stem}{suffix}"
    counter = 1
    while candidate.exists():
        candidate = directory / f"{timestamp}_{filename_stem}_{counter:02d}{suffix}"
        counter += 1
    return candidate


def page_url(base_url: str, page_number: int) -> str:
    return base_url if page_number == 1 else f"{base_url}/{page_number}"


def parse_identity(href: str) -> tuple[str, str] | None:
    """IssueLink 이동 링크의 경로에서 사이트와 게시글 ID만 추출한다."""
    parts = [part for part in urlparse(href).path.split("/") if part]
    try:
        community_index = parts.index("community")
        if parts[community_index + 1] != "go":
            return None
        site = parts[community_index + 2]
        post_id = parts[community_index + 3]
    except (ValueError, IndexError):
        return None
    return (site, post_id) if site and post_id else None


def parse_integer(value: object) -> int:
    return int(re.sub(r"[^0-9]", "", str(value)) or 0)


def parse_title_and_comments(title: str) -> tuple[str, int]:
    match = re.search(r"\s*\[\s*([0-9][0-9,]*)\s*\]\s*$", title)
    if match is None:
        return title.strip(), 0
    return title[: match.start()].strip(), parse_integer(match.group(1))


def parse_row(raw_row: Mapping[str, object]) -> dict[str, object] | None:
    identity = parse_identity(str(raw_row.get("href", "")))
    if identity is None:
        return None

    site, post_id = identity
    title, comments = parse_title_and_comments(str(raw_row.get("title", "")))
    return {
        "사이트": site,
        "id값": post_id,
        "제목": title,
        "작성시간": str(raw_row.get("date", "")).strip(),
        "댓글수": comments,
        "조회수": parse_integer(raw_row.get("hits", "")),
        "원문URL": "",
    }


def extract_listing_rows(page: Page, url: str, page_number: int) -> list[dict[str, object]]:
    LOGGER.info("목록 페이지 요청: page=%s url=%s", page_number, url)
    page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    try:
        page.locator(POST_LINK_SELECTOR).first.wait_for(
            state="visible", timeout=30_000
        )
    except PlaywrightTimeoutError:
        LOGGER.warning("게시글 링크를 찾지 못함: page=%s url=%s", page_number, url)
        return []

    raw_rows = page.locator("table tr").evaluate_all(
        """rows => rows.map(row => {
            const link = row.querySelector("a[href*='/community/go/']");
            if (!link) return null;
            return {
                href: link.getAttribute("href") || "",
                title: link.textContent?.trim() || "",
                date: row.querySelector(".second_date span")?.textContent?.trim() || "",
                hits: row.querySelector(".hit")?.textContent?.trim() || ""
            };
        }).filter(Boolean)"""
    )

    rows: list[dict[str, object]] = []
    for raw_row in raw_rows:
        parsed = parse_row(raw_row)
        if parsed is not None:
            rows.append(parsed)
    LOGGER.info("목록 페이지 파싱 완료: page=%s rows=%s", page_number, len(rows))
    return rows


def block_unneeded_resources(route: Any) -> None:
    if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
        route.abort()
    else:
        route.continue_()


def add_record(
    records: dict[tuple[str, str], dict[str, object]],
    row: dict[str, object],
    source: str,
    score: int,
) -> None:
    key = (str(row["사이트"]), str(row["id값"]))
    source_entry = {"source": source, "score": score}
    existing = records.get(key)
    if existing is None:
        record = dict(row)
        record["수집처"] = [source_entry]
        records[key] = record
        return

    sources = existing.setdefault("수집처", [])
    if not isinstance(sources, list):
        sources = []
        existing["수집처"] = sources
    sources.append(source_entry)


def save_json(path: Path, records: Iterable[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(list(records), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def log_statistics(
    records: list[dict[str, object]],
    discovered_count: int,
    completed_pages: int,
    pages_with_rows: int,
    requested_pages: int,
) -> None:
    community_counts = Counter(
        str(record.get("사이트", "")).strip()
        for record in records
        if str(record.get("사이트", "")).strip()
    )
    LOGGER.info("커뮤니티별 수집 통계")
    for community, count in sorted(community_counts.items(), key=lambda item: (-item[1], item[0])):
        LOGGER.info("- %s: %s개", community, count)
    LOGGER.info("전체 고유 게시글: %s개", len(records))
    LOGGER.info("전체 목록 발견 건수(중복 포함): %s개", discovered_count)
    LOGGER.info("페이지 요청 완료: %s/%s개", completed_pages, requested_pages)
    LOGGER.info("게시글 확인 페이지: %s/%s개", pages_with_rows, requested_pages)


def crawl(started_at: datetime, headed: bool, output_path: Path) -> int:
    records: dict[tuple[str, str], dict[str, object]] = {}
    discovered_count = 0
    completed_pages = 0
    pages_with_rows = 0
    requested_pages = len(SOURCE_LISTS) * PAGES_PER_LIST

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not headed)
        context = browser.new_context(user_agent=USER_AGENT, locale="ko-KR")
        context.route("**/*", block_unneeded_resources)
        page = context.new_page()
        try:
            for source, base_url in SOURCE_LISTS:
                source_score = 0
                LOGGER.info("수집 시작: source=%s pages=%s", source, PAGES_PER_LIST)
                for page_number in range(1, PAGES_PER_LIST + 1):
                    current_url = page_url(base_url, page_number)
                    try:
                        rows = extract_listing_rows(page, current_url, page_number)
                    except Exception as error:
                        LOGGER.error(
                            "목록 페이지 수집 실패: source=%s page=%s error=%s",
                            source,
                            page_number,
                            error,
                        )
                        continue

                    completed_pages += 1
                    if rows:
                        pages_with_rows += 1
                    for row in rows:
                        source_score += 1
                        discovered_count += 1
                        add_record(records, row, source, source_score)

                    if page_number < PAGES_PER_LIST:
                        time.sleep(random.uniform(0.8, 1.6))
                LOGGER.info(
                    "수집 종료: source=%s discovered=%s unique_total=%s",
                    source,
                    source_score,
                    len(records),
                )
        finally:
            context.close()
            browser.close()

    final_records = list(records.values())
    save_json(output_path, final_records)
    LOGGER.info("JSON 저장 완료: %s", output_path.resolve())
    log_statistics(
        final_records,
        discovered_count,
        completed_pages,
        pages_with_rows,
        requested_pages,
    )
    LOGGER.info("실행 시작 시각: %s", started_at.strftime("%Y-%m-%d %H:%M:%S"))
    return 0 if completed_pages == requested_pages else 1


def main(argv: list[str] | None = None) -> int:
    started_at = datetime.now()
    timestamp = started_at.strftime("%Y%m%d_%H%M")
    args = parse_args(argv)

    output_path = timestamped_path(
        args.output,
        RESULT_DIR,
        timestamp,
        "issuelink_multi_list_result",
        ".json",
    )
    log_path = timestamped_path(
        None,
        LOG_DIR,
        timestamp,
        "issuelink_multi_list_crawler",
        ".log",
    )
    configure_logging(log_path, args.log_level)
    LOGGER.info("실행 시작: headed=%s requested_pages=%s", args.headed, len(SOURCE_LISTS) * PAGES_PER_LIST)

    try:
        return crawl(started_at, args.headed, output_path)
    except Exception as error:
        LOGGER.exception("IssueLink 수집 실패: %s", error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

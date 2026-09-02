"""IssueLink 목록을 수집해 원문 URL이 포함된 JSON으로 누적 저장한다.

목록은 Playwright로 읽고, 각 IssueLink 응답의 리디렉션 주소만 읽어 원문 URL로
저장한다. 원문 사이트에는 요청하지 않는다. 실행을 다시 해도 ``사이트 + id값``이
같은 게시글은 저장하지 않는다.
출력은 고정 JSON 파일 하나에 원자적으로 누적·갱신한다.

설치:
    python -m pip install playwright requests
    python -m playwright install chromium

예시:
    python test_spark/20260902_1716_issuelink_realtime_crawler.py --max-new-posts 1000
    python test_spark/20260902_1716_issuelink_realtime_crawler.py --headed --max-pages 3
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


LIST_URL = "https://www.issuelink.co.kr/community/listview/all/48/click/_self/blank/blank/blank"
ISSUELINK_ORIGIN = "https://www.issuelink.co.kr"
SPARK_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = SPARK_DIR / "result" / "issuelink_posts.json"
OUTPUT_PATH = OUTPUT_DIR / "20260902_1721_issuelink_posts.json"
LOG_DIR = SPARK_DIR / "log"
FIELDS = ["사이트", "id값", "제목", "작성시간", "댓글수", "조회수", "원문URL"]
BLOCKED_RESOURCE_TYPES = {"font", "image", "media", "stylesheet"}
MAX_REDIRECT_ATTEMPTS = 3
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
LOGGER = logging.getLogger("issuelink_realtime_crawler")


def create_log_path(started_at: datetime) -> Path:
    """실행 시각을 포함한 새 로그 파일 경로를 만든다."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    prefix = f"{started_at:%Y%m%d_%H%M}_issuelink_realtime_crawler"
    candidate = LOG_DIR / f"{prefix}.log"
    suffix = 1
    while True:
        try:
            candidate.open("x", encoding="utf-8").close()
            return candidate
        except FileExistsError:
            candidate = LOG_DIR / f"{prefix}_{suffix:02d}.log"
            suffix += 1


def configure_logging(started_at: datetime) -> Path:
    """콘솔과 타임스탬프 로그 파일에 같은 실행 기록을 남긴다."""
    log_path = create_log_path(started_at)
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    file_handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    console_handler = logging.StreamHandler(sys.stdout)
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)
    LOGGER.addHandler(file_handler)
    LOGGER.addHandler(console_handler)
    LOGGER.info("로그 파일 시작: %s", log_path.resolve())
    return log_path


def parse_identity(href: str) -> tuple[str, str] | None:
    """IssueLink 이동 링크에서 사이트와 게시글 ID를 꺼낸다."""
    parts = [part for part in urlparse(href).path.split("/") if part]
    try:
        index = parts.index("community")
        if parts[index + 1] != "go":
            return None
        site, post_id = parts[index + 2], parts[index + 3]
    except (ValueError, IndexError):
        return None
    return (site, post_id) if site and post_id else None


def parse_integer(value: str) -> int:
    return int(re.sub(r"[^0-9]", "", value) or 0)


def parse_title_and_comments(title: str) -> tuple[str, int]:
    match = re.search(r"\s*\[\s*([0-9][0-9,]*)\s*\]\s*$", title)
    if not match:
        return title.strip(), 0
    return title[: match.start()].strip(), parse_integer(match.group(1))


def parse_written_at(value: str) -> datetime | None:
    """목록의 절대 날짜를 보존/만료 판정에 사용할 datetime으로 변환한다."""
    value = value.strip()
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y.%m.%d %H:%M"):
        try:
            return datetime.strptime(value, pattern)
        except ValueError:
            pass
    return None


def load_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"기존 JSON을 읽을 수 없습니다: {path} ({error})") from error
    if not isinstance(payload, list):
        raise SystemExit(f"JSON 최상위 값은 배열이어야 합니다: {path}")
    return [row for row in payload if isinstance(row, dict)]


def load_existing_records(output_dir: Path, output_path: Path) -> tuple[list[dict[str, Any]], Path | None]:
    """고정 파일 전환 시 가장 최신의 유효 스냅샷을 한 번만 가져온다."""
    snapshots = sorted(output_dir.glob("*_issuelink_posts.json"), reverse=True)
    if output_path not in snapshots and output_path.exists():
        snapshots.append(output_path)
    for path in snapshots:
        try:
            return load_records(path), path
        except SystemExit as error:
            LOGGER.warning("유효하지 않은 기존 JSON을 건너뜀: %s (%s)", path, error)
    return [], None


def cleanup_legacy_outputs(output_dir: Path, output_path: Path) -> None:
    """고정 파일을 안전하게 쓴 뒤 이전 스냅샷과 남은 임시 파일을 정리한다."""
    if not output_dir.exists():
        return
    patterns = ("*_issuelink_posts.json", "*_issuelink_posts.tmp")
    for pattern in patterns:
        for path in output_dir.glob(pattern):
            if path == output_path:
                continue
            try:
                path.unlink()
                LOGGER.info("이전 출력 정리: %s", path.name)
            except OSError as error:
                LOGGER.warning(
                    "이전 출력 정리 실패, 다음 실행에 재시도: %s (%s)", path, error
                )


def write_records(path: Path, records: list[dict[str, Any]]) -> None:
    """중간 종료에도 JSON 손상을 피하도록 임시 파일 뒤 원자적으로 교체한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(
        json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    # Windows 보안 프로그램/검색 인덱서가 잠깐 파일을 잡는 경우가 있어 재시도한다.
    last_error: PermissionError | None = None
    for attempt in range(10):
        try:
            temp_path.replace(path)
            return
        except PermissionError as error:
            last_error = error
            time.sleep(0.25 * (attempt + 1))
    raise last_error or PermissionError(f"출력 파일 교체 실패: {path}")


def remove_expired(
    records: list[dict[str, Any]], retention_hours: int, now: datetime | None = None
) -> list[dict[str, Any]]:
    return [
        record
        for record in records
        if not is_expired(record, retention_hours, now)
    ]


def is_expired(
    record: dict[str, Any], retention_hours: int, now: datetime | None = None
) -> bool:
    """보관 기준을 넘긴 레코드인지 판정한다.

    날짜를 해석할 수 없는 항목은 삭제 대상으로 단정하지 않고 유지한다.
    """
    written_at = parse_written_at(str(record.get("작성시간", "")))
    if written_at is None:
        return False
    reference_time = now or datetime.now()
    return written_at < reference_time - timedelta(hours=retention_hours)


def record_key(record: dict[str, Any]) -> tuple[str, str] | None:
    site = str(record.get("사이트", "")).strip()
    post_id = str(record.get("id값", "")).strip()
    return (site, post_id) if site and post_id else None


def list_page_url(page_number: int) -> str:
    return LIST_URL if page_number == 1 else f"{LIST_URL}/{page_number}"


def extract_listing(
    page, page_number: int, retention_hours: int | None = None
) -> tuple[list[dict[str, Any]], int]:
    page.goto(list_page_url(page_number), wait_until="domcontentloaded", timeout=30_000)
    page.locator("table tr a[href*='/community/go/']").first.wait_for(
        state="visible", timeout=30_000
    )
    rows: list[dict[str, Any]] = []
    expired_count = 0
    # 행마다 Playwright 왕복 호출을 하지 않고, 브라우저 안에서 목록 필드를 한 번에 읽는다.
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
    for raw_row in raw_rows:
        href = raw_row["href"]
        if retention_hours is not None and is_expired(
            {"작성시간": raw_row["date"]}, retention_hours
        ):
            # 만료 항목은 제목·ID·댓글·조회수 파싱과 원문 URL 요청을 모두 생략한다.
            expired_count += 1
            continue
        identity = parse_identity(href)
        if identity is None:
            continue
        site, post_id = identity
        title, comments = parse_title_and_comments(raw_row["title"])
        rows.append(
            {
                "사이트": site,
                "id값": post_id,
                "제목": title,
                "작성시간": raw_row["date"],
                "댓글수": comments,
                "조회수": parse_integer(raw_row["hits"]),
                "_issue_link": urljoin(ISSUELINK_ORIGIN, href),
            }
        )
    return rows, expired_count


def retry_delay(attempt: int) -> float:
    """네트워크 요청 실패 시 짧지만 점진적으로 길어지는 대기 시간을 만든다."""
    return random.uniform(0.7, 1.1) * (2**attempt)


def resolve_original_url(session: requests.Session, issue_link: str) -> str | None:
    """원문 사이트에 요청하지 않고 IssueLink의 리디렉션 주소만 반환한다."""
    for attempt in range(MAX_REDIRECT_ATTEMPTS):
        try:
            response = session.get(issue_link, timeout=(5, 20), allow_redirects=False)
            location = response.headers.get("Location", "").strip()
            if not location:
                LOGGER.warning("원문 주소 없음, 건너뜀: %s (Location 헤더 없음)", issue_link)
                return None
            return urljoin(issue_link, location)
        except requests.RequestException as error:
            if attempt + 1 == MAX_REDIRECT_ATTEMPTS:
                LOGGER.warning("IssueLink 주소 요청 실패, 건너뜀: %s (%s)", issue_link, error)
                return None
            delay = retry_delay(attempt)
            LOGGER.warning(
                "IssueLink 주소 요청 재시도 %s/%s: %s (%s), %.1f초 대기",
                attempt + 1,
                MAX_REDIRECT_ATTEMPTS - 1,
                issue_link,
                error,
                delay,
            )
            time.sleep(delay)
    return None


def block_unneeded_resources(route) -> None:
    if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
        route.abort()
    else:
        route.continue_()


def main() -> None:
    started_at = datetime.now()
    parser = argparse.ArgumentParser(description="IssueLink 실시간 JSON 누적 크롤러")
    parser.add_argument("--max-new-posts", type=int, default=1000, help="이번 실행의 최대 신규 저장 수")
    parser.add_argument("--max-pages", type=int, default=100, help="최대 목록 페이지 수")
    parser.add_argument("--retention-hours", type=int, default=48, help="보관 시간(기본 48시간)")
    parser.add_argument("--headed", action="store_true", help="브라우저 창 표시")
    args = parser.parse_args()
    if args.max_new_posts < 1 or args.max_pages < 1 or args.retention_hours < 0:
        raise SystemExit("max-new-posts, max-pages는 1 이상이고 retention-hours는 0 이상이어야 합니다.")
    log_path = configure_logging(started_at)
    LOGGER.info(
        "실행 시작: max_new_posts=%s, max_pages=%s, retention_hours=%s, headed=%s",
        args.max_new_posts,
        args.max_pages,
        args.retention_hours,
        args.headed,
    )

    output_path = OUTPUT_PATH
    records, source_path = load_existing_records(OUTPUT_DIR, output_path)
    retained = remove_expired(records, args.retention_hours)
    removed = len(records) - len(retained)
    # 수집 전 만료 삭제 결과를 즉시 저장한 뒤, 이전 실행별 스냅샷을 정리한다.
    write_records(output_path, retained)
    cleanup_legacy_outputs(OUTPUT_DIR, output_path)
    known_keys = {key for row in retained if (key := record_key(row)) is not None}
    source_message = str(source_path) if source_path else "없음"
    LOGGER.info("기존 데이터: %s", source_message)
    LOGGER.info("만료 삭제 완료: %s개 삭제, %s개 유지", removed, len(retained))

    started_perf = time.perf_counter()
    saved = 0
    updated = 0
    duplicates = 0
    listed = 0
    request_failures = 0
    expired_skipped = 0
    page_timings: list[tuple[int, float, int]] = []
    record_indexes = {
        key: index
        for index, row in enumerate(retained)
        if (key := record_key(row)) is not None
    }
    with requests.Session() as session:
        session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "ko-KR,ko;q=0.9"})
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=not args.headed)
                context = browser.new_context(user_agent=USER_AGENT, locale="ko-KR")
                context.route("**/*", block_unneeded_resources)
                page = context.new_page()
                try:
                    for page_number in range(1, args.max_pages + 1):
                        page_started = time.perf_counter()
                        page_new = 0
                        page_updated = 0
                        page_changed = False
                        candidates: list[dict[str, Any]] = []
                        try:
                            candidates, page_expired = extract_listing(
                                page, page_number, args.retention_hours
                            )
                            expired_skipped += page_expired
                            listed += len(candidates) + page_expired
                            if not candidates and not page_expired:
                                break
                            if not candidates:
                                elapsed = time.perf_counter() - page_started
                                page_timings.append((page_number, elapsed, page_expired))
                                LOGGER.info(
                                    "목록 %s/%s: %s개 만료 스킵, %.1f초",
                                    page_number,
                                    args.max_pages,
                                    page_expired,
                                    elapsed,
                                )
                                if saved >= args.max_new_posts:
                                    break
                                time.sleep(random.uniform(0.7, 1.2))
                                continue
                            for candidate in candidates:
                                key = (candidate["사이트"], candidate["id값"])
                                if key in known_keys:
                                    duplicates += 1
                                    existing_index = record_indexes[key]
                                    existing = retained[existing_index]
                                    changed = False
                                    for field in ("제목", "작성시간", "댓글수", "조회수"):
                                        if existing.get(field) != candidate[field]:
                                            existing[field] = candidate[field]
                                            changed = True
                                    if changed:
                                        page_changed = True
                                        updated += 1
                                        page_updated += 1
                                    continue
                                issue_link = candidate.pop("_issue_link")
                                original_url = resolve_original_url(session, issue_link)
                                if original_url is None:
                                    request_failures += 1
                                    continue
                                LOGGER.info("원문 주소 확보: %s -> %s", issue_link, original_url)
                                candidate["원문URL"] = original_url
                                record = {field: candidate[field] for field in FIELDS}
                                retained.append(record)
                                known_keys.add(key)
                                record_indexes[key] = len(retained) - 1
                                page_changed = True
                                saved += 1
                                page_new += 1
                                LOGGER.info(
                                    "저장 %s/%s: %s/%s",
                                    saved,
                                    args.max_new_posts,
                                    key[0],
                                    key[1],
                                )
                                if saved >= args.max_new_posts:
                                    break
                                time.sleep(random.uniform(0.5, 0.8))
                        finally:
                            # Ctrl+C나 예상 밖의 오류가 나도 현재 페이지의 변경분은 보존한다.
                            if page_changed:
                                write_records(output_path, retained)
                        elapsed = time.perf_counter() - page_started
                        page_timings.append((page_number, elapsed, len(candidates)))
                        LOGGER.info(
                            "목록 %s/%s: %s개, 신규 %s개, 갱신 %s개, %.1f초",
                            page_number,
                            args.max_pages,
                            len(candidates),
                            page_new,
                            page_updated,
                            elapsed,
                        )
                        if saved >= args.max_new_posts:
                            break
                        time.sleep(random.uniform(0.7, 1.2))
                finally:
                    context.close()
                    browser.close()
        except PlaywrightTimeoutError as error:
            raise SystemExit(f"목록 로딩 시간 초과: {error}") from error

    elapsed = time.perf_counter() - started_perf
    rate = listed / elapsed if elapsed else 0.0
    LOGGER.info(
        "완료: %.1f초 (%.1f분), 목록 %s개, 신규 %s개, 갱신 %s개, 중복 %s개, "
        "만료 스킵 %s개, 원문 주소 확보 실패 %s개, JSON 총 %s개, 처리속도 %.2f건/초",
        elapsed,
        elapsed / 60,
        listed,
        saved,
        updated,
        duplicates,
        expired_skipped,
        request_failures,
        len(retained),
        rate,
    )
    LOGGER.info("저장 위치: %s", output_path.resolve())
    LOGGER.info("실행 로그 위치: %s", log_path.resolve())


if __name__ == "__main__":
    main()

"""Collect FMKorea comments for one post using a single Selenium session."""

from __future__ import annotations

import argparse
import math
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, Tag
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from src.common.files import atomic_write_json

from .browser import create_chrome_driver


MAX_PAGE = 7
VIEW_COUNT_LIMIT = 10_000
MIN_DELAY_SECONDS = 5.0
MAX_DELAY_SECONDS = 8.0
PAGE_TIMEOUT_SECONDS = 45
DOM_TIMEOUT_SECONDS = 35
POST_URL_TEMPLATE = "https://www.fmkorea.com/{post_id}"
COMMENT_WRAPPER_SELECTOR = ".fdb_lst_wrp"
BLOCK_MARKERS = (
    "captcha",
    "recaptcha",
    "hcaptcha",
    "cloudflare",
    "access denied",
    "forbidden",
    "too many requests",
    "just a moment",
    "service unavailable",
    "bad gateway",
    "sign in",
    "log in",
)
POST_NOT_FOUND_MARKER = "해당 문서가 존재하지 않습니다."
DELETED_COMMENT_TEXTS = frozenset({"삭제된 댓글입니다.", "[삭제된 댓글입니다.]"})
TITLE_SELECTORS = (".np_18px", ".rd_title h1", ".rd_title", "h1")
BODY_SELECTORS = (".rd_body .xe_content", ".rd_body article", ".document .xe_content", "article .xe_content")
VIEW_COUNT_SELECTORS = (".readNum", ".side.fr > span:nth-child(1) > b")
NON_TEXT_TAGS = "script, style, noscript, template"
BLOCKED_ERROR_MESSAGE = "FMKorea access was blocked; no retry was attempted"


class CrawlError(RuntimeError):
    """Raised when a page cannot be safely collected."""


def validate_post_id(value: str) -> int:
    if not value.isdigit() or int(value) <= 0:
        raise CrawlError("post_id must be a positive integer")
    return int(value)


def normalize_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def select_text(node: Tag, selector: str) -> str:
    """Return the normalized text of the first match, or an empty string."""
    found = node.select_one(selector)
    return normalize_text(found.get_text(" ", strip=True)) if found else ""


def parse_comment_id(value: str | None) -> int | None:
    match = re.search(r"comment_(\d+)", value or "")
    return int(match.group(1)) if match else None


def parse_number(value: str | None) -> int:
    match = re.search(r"[+-]?\d[\d,]*", value or "")
    return int(match.group(0).replace(",", "")) if match else 0


def parse_comment_content(item: Tag) -> str:
    content_node = item.select_one(".comment-content .xe_content")
    if content_node is None:
        return ""
    for parent_link in content_node.select("a.findParent"):
        parent_link.decompose()
    return normalize_text(content_node.get_text(" ", strip=True))


def parse_view_count(html: str) -> int:
    """Parse the post view count from the first-page HTML."""
    soup = BeautifulSoup(html, "html.parser")
    for selector in VIEW_COUNT_SELECTORS:
        for node in soup.select(selector):
            text = normalize_text(node.get_text(" ", strip=True))
            if re.search(r"\d", text):
                return parse_number(text)
    raise CrawlError("FMKorea view count was not found")


def is_blocked_html(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    if soup.select_one(COMMENT_WRAPPER_SELECTOR):
        return False
    for node in soup.select(NON_TEXT_TAGS):
        node.decompose()
    visible = normalize_text(soup.get_text(" ", strip=True)).lower()
    return any(marker in visible for marker in BLOCK_MARKERS)


def is_post_not_found_html(html: str) -> bool:
    return POST_NOT_FOUND_MARKER in html


def is_deleted_comment(content: str) -> bool:
    return content in DELETED_COMMENT_TEXTS or bool(
        re.fullmatch(r"deleted comment", content, re.IGNORECASE)
    )


def parse_comment_depth(style: str) -> int:
    """FMKorea indents each reply level by a 2% left margin."""
    match = re.search(r"margin-left\s*:\s*([\d.]+)%", style, re.IGNORECASE)
    return max(0, round(float(match.group(1)) / 2)) if match else 0


def parse_comment_item(item: Tag, position: int, post_url: str) -> dict[str, Any] | None:
    comment_id = parse_comment_id(item.get("id"))
    if comment_id is None:
        return None
    parent_link = item.select_one(".comment-content a.findParent")
    parent_id = parse_comment_id(parent_link.get("href") if parent_link else None)
    content = parse_comment_content(item)
    is_deleted = is_deleted_comment(content)
    clipboard = item.select_one("a[data-clipboard-text*='#comment_']")
    raw_comment_url = clipboard.get("data-clipboard-text") if clipboard else None
    depth = parse_comment_depth(item.get("style", ""))
    return {
        "comment_id": comment_id,
        "content": None if is_deleted else (content or None),
        "is_deleted": is_deleted,
        "parent_id": parent_id,
        "author": select_text(item, ".meta a.member_plate") or None,
        "up_votes": parse_number(select_text(item, ".voted_count")),
        "down_votes": parse_number(select_text(item, ".blamed_count")),
        "date_text": select_text(item, ".meta .date") or None,
        "comment_url": raw_comment_url or f"{post_url}#comment_{comment_id}",
        "position": position,
        "depth": depth,
        "is_reply": depth > 0,
    }


def parse_comment_page(html: str, post_url: str) -> tuple[list[dict[str, Any]], int]:
    """Return the page's comments and the total number of comment pages."""
    if is_post_not_found_html(html):
        return [], 1
    soup = BeautifulSoup(html, "html.parser")
    wrapper = soup.select_one(COMMENT_WRAPPER_SELECTOR)
    if wrapper is None:
        if is_blocked_html(html):
            raise CrawlError(BLOCKED_ERROR_MESSAGE)
        raise CrawlError("FMKorea comment wrapper was not found")

    linked_pages = [
        int(match.group(1))
        for link in wrapper.select(".fdb_tag a[href]")
        if (match := re.search(r"[?&]cpage=(\d+)", link.get("href", "")))
    ]
    total_comments = parse_number(select_text(wrapper, ".fdb_tag a"))
    items = wrapper.select("ul.fdb_lst_ul > li[id^='comment_']")
    comments = [
        comment
        for position, item in enumerate(items, start=1)
        if (comment := parse_comment_item(item, position, post_url)) is not None
    ]

    estimated_pages = math.ceil(total_comments / len(comments)) if total_comments and comments else 1
    total_pages = max(max(linked_pages, default=1), estimated_pages)
    return comments, max(1, total_pages)


def _first_text(soup: BeautifulSoup, selectors: tuple[str, ...]) -> str:
    for selector in selectors:
        if text := select_text(soup, selector):
            return text
    return ""


def parse_post_content(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    title = _first_text(soup, TITLE_SELECTORS)
    if not title:
        meta = soup.select_one('meta[property="og:title"], meta[name="title"]')
        title = normalize_text(meta.get("content") if meta else "")
    if not title and soup.title:
        title = normalize_text(soup.title.get_text(" ", strip=True))

    body = ""
    for selector in BODY_SELECTORS:
        node = soup.select_one(selector)
        if node:
            for removable in node.select(f"{NON_TEXT_TAGS}, img, video, audio, iframe"):
                removable.decompose()
            body = normalize_text(node.get_text(" ", strip=True))
            if body:
                break
    return title, body


def load_comment_page(driver: webdriver.Chrome, url: str, page_number: int) -> str:
    """Open one comment page and return its HTML once the comments are usable."""
    print(f"[selenium] requesting page {page_number}/{MAX_PAGE}: {url}")
    try:
        driver.get(url)
    except TimeoutException:
        # Eager loading may time out after the useful DOM is available.
        pass
    except WebDriverException as error:
        raise CrawlError(f"browser request failed on page {page_number}: {error}") from error

    try:
        WebDriverWait(driver, DOM_TIMEOUT_SECONDS).until(
            lambda current: current.find_elements(By.CSS_SELECTOR, COMMENT_WRAPPER_SELECTOR)
            or current.execute_script("return document.readyState") == "complete"
        )
    except TimeoutException:
        if is_blocked_html(driver.page_source):
            raise CrawlError(BLOCKED_ERROR_MESSAGE)
        raise CrawlError(f"comment DOM did not load on page {page_number}")
    return driver.page_source


def page_entry(page_number: int, url: str, comments: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "page_number": page_number,
        "url": url,
        "comment_count": len(comments),
        "comments": comments,
    }


def fetch_comments(post_id: int) -> dict[str, Any]:
    post_url = POST_URL_TEMPLATE.format(post_id=post_id)
    driver = create_chrome_driver(page_load_timeout_seconds=PAGE_TIMEOUT_SECONDS)
    pages: list[dict[str, Any]] = []
    unique_comments: dict[int, dict[str, Any]] = {}
    post_title = ""
    post_body = ""
    view_count: int | None = None
    post_status = "ok"

    try:
        for page_number in range(1, MAX_PAGE + 1):
            url = f"{post_url}?cpage={page_number}"
            page_html = load_comment_page(driver, url, page_number)
            not_found = is_post_not_found_html(page_html)
            if page_number == 1:
                if is_blocked_html(page_html):
                    raise CrawlError(BLOCKED_ERROR_MESSAGE)
                post_title, post_body = parse_post_content(page_html)
                if not not_found:
                    view_count = parse_view_count(page_html)
                    # Low-traffic posts are skipped to keep the request volume small.
                    if view_count <= VIEW_COUNT_LIMIT:
                        pages.append(page_entry(page_number, url, []))
                        print(
                            f"[selenium] view count {view_count:,} <= {VIEW_COUNT_LIMIT:,}; "
                            "skipping comments"
                        )
                        break
            if not_found:
                post_status = "not_found"
            page_comments, total_pages = parse_comment_page(page_html, post_url)
            pages.append(page_entry(page_number, url, page_comments))
            for comment in page_comments:
                if comment["content"] and not comment["is_deleted"]:
                    unique_comments.setdefault(comment["comment_id"], comment)

            if page_number >= total_pages or page_number >= MAX_PAGE:
                break
            time.sleep(random.uniform(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS))
    finally:
        driver.quit()

    return {
        "schema_version": 1,
        "status": post_status,
        "post_id": str(post_id),
        "post_url": post_url,
        "title": post_title,
        "body": post_body,
        "view_count": view_count,
        "pages_requested": MAX_PAGE,
        "pages_crawled": len(pages),
        "comment_count": len(unique_comments),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pages": pages,
        "comments": list(unique_comments.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect FMKorea comments from pages 1 through 7")
    parser.add_argument("--post-id", required=True, help="FMKorea post number")
    parser.add_argument("-o", "--output", type=Path, default=Path("result.json"))
    args = parser.parse_args()
    try:
        post_id = validate_post_id(args.post_id)
        payload = fetch_comments(post_id)
        atomic_write_json(args.output, payload)
        print(f"saved {payload['comment_count']} comments to {args.output}")
        return 0
    except Exception as error:
        print(f"FMKorea comment crawl failed: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

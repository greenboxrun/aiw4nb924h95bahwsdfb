"""Headless Chrome setup shared by the FMKorea Selenium crawlers."""

from __future__ import annotations

from selenium import webdriver
from selenium.webdriver.chrome.options import Options


DEFAULT_PAGE_LOAD_TIMEOUT_SECONDS = 45


def create_chrome_driver(
    *,
    user_agent: str | None = None,
    page_load_timeout_seconds: int = DEFAULT_PAGE_LOAD_TIMEOUT_SECONDS,
) -> webdriver.Chrome:
    """Create a headless Chrome that works both locally and on GitHub Actions."""
    options = Options()
    options.page_load_strategy = "eager"
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--lang=ko-KR")
    if user_agent:
        options.add_argument(f"--user-agent={user_agent}")
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(page_load_timeout_seconds)
    return driver

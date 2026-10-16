"""Runtime settings shared by the IssueLink browser clients."""

SOURCE_LISTS = (
    (
        "issuelink_read",
        "https://www.issuelink.co.kr/community/listview/all/48/read/_self/blank/blank/blank",
    ),
    (
        "issuelink_comment",
        "https://www.issuelink.co.kr/community/listview/all/48/comment/_self/blank/blank/blank",
    ),
)
PAGES_PER_LIST = 10
ISSUELINK_ORIGIN = "https://www.issuelink.co.kr"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
BLOCKED_RESOURCE_TYPES = {"font", "image", "media", "stylesheet"}
LISTING_LINK_SELECTOR = "table tr a[href*='/community/go/']"
# Listing loads and redirect lookups keep retrying until this window
# (measured from crawl start) closes.
RETRY_WINDOW_SECONDS = 18 * 60
RETRY_DELAY_SECONDS = (2.0, 5.0)
FULL_DIAGNOSTIC_EVERY = 10
# Redirect lookups run once per post, so cap them even inside the retry window.
MAX_REDIRECT_ATTEMPTS = 13
# A healthy listing renders within a few seconds; fail fast so retries come sooner.
LISTING_GOTO_TIMEOUT_SECONDS = 15
LISTING_LINK_TIMEOUT_SECONDS = 10
LISTING_PAGE_DELAY_SECONDS = (2.0, 4.0)
DIAGNOSTIC_BODY_BYTES = 2048

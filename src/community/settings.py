"""Runtime settings shared by the IssueLink browser clients."""

LIST_URL = (
    "https://www.issuelink.co.kr/community/listview/all/48/"
    "click/_self/blank/blank/blank"
)
ISSUELINK_ORIGIN = "https://www.issuelink.co.kr"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
BLOCKED_RESOURCE_TYPES = {"font", "image", "media", "stylesheet"}
MAX_REDIRECT_ATTEMPTS = 2
DIAGNOSTIC_BODY_BYTES = 2048

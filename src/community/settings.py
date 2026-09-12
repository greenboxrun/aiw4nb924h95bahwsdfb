"""Runtime settings shared by the IssueLink browser clients."""

LIST_URL = (
    "https://www.issuelink.co.kr/community/listview/all/48/"
    "click/_self/blank/blank/blank"
)
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
    ("issuelink_click", LIST_URL),
)
PAGES_PER_LIST = 10
TITLE_EXCLUDE_KEYWORDS = ("ㅇㅎ", "후방", "약후", "ㅎㅂ")
ISSUELINK_ORIGIN = "https://www.issuelink.co.kr"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
BLOCKED_RESOURCE_TYPES = {"font", "image", "media", "stylesheet"}
MAX_REDIRECT_ATTEMPTS = 2
DIAGNOSTIC_BODY_BYTES = 2048

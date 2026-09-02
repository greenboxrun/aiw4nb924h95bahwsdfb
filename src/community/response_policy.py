"""Pure response inspection rules for IssueLink redirects."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin, urlparse


def extract_redirect_url(response_url: str, headers: dict[str, str], body: str) -> str | None:
    """Extract a redirect target without following it to the source site."""
    location = headers.get("location", "").strip()
    if location:
        return urljoin(response_url, location)
    refresh = headers.get("refresh", "")
    refresh_match = re.search(r"(?:^|;)\s*url\s*=\s*([^;]+)", refresh, re.IGNORECASE)
    if refresh_match:
        return urljoin(response_url, refresh_match.group(1).strip(" '\""))
    patterns = (
        r"<meta[^>]+http-equiv\s*=\s*['\"]?refresh['\"]?[^>]+content\s*=\s*['\"][^'\"]*url\s*=\s*([^'\"]+)",
        r"(?:window\.)?location(?:\.href|\.replace|\.assign)?\s*\(?'?\s*['\"]([^'\"]+)['\"]",
    )
    for pattern in patterns:
        match = re.search(pattern, body, re.IGNORECASE)
        if match:
            return urljoin(response_url, match.group(1).strip())
    return None


def detect_challenge(body: str, headers: dict[str, str] | None = None) -> str | None:
    lowered = body.lower()
    markers = ("cupid.js", "slowaes.decrypt", 'document.cookie="cupid=', "cupid=")
    header_text = ""
    if headers:
        header_text = " ".join(f"{key}:{value}" for key, value in headers.items()).lower()
    return "cupid" if any(marker in lowered for marker in markers) or "cupid" in header_text else None


def is_issuelink_go_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.netloc.lower().endswith("issuelink.co.kr") and parsed.path.startswith("/community/go/")


def set_cookie_names(header: str) -> list[str]:
    if not header:
        return []
    return sorted(set(re.findall(r"(?:^|,\s*)([!#$%&'*+\-.^_`|~0-9A-Za-z]+)=", header)))


def diagnostic_headers(headers: Any) -> dict[str, str]:
    """Keep useful response headers while excluding cookies and auth values."""
    names = ("location", "refresh", "content-type", "server", "via", "x-cache", "x-cache-hits", "cf-cache-status", "retry-after")
    lowered = {str(key).lower(): str(value) for key, value in headers.items()}
    return {name: lowered[name] for name in names if name in lowered}

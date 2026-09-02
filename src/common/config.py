"""Shared configuration and local credential loading."""

from __future__ import annotations

import os

from .paths import PROJECT_ROOT

OWNER = "greenboxrun"
REPOSITORY = "aiw4nb924h95bahwsdfb"
REF = "master"
API_BASE = "https://api.github.com"
API_VERSION = "2022-11-28"
def load_token() -> str:
    """Load GITHUB_TOKEN, falling back to the ignored local github.env file."""
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        return token
    env_path = PROJECT_ROOT / "github.env"
    if not env_path.is_file():
        raise ValueError(f"GitHub token file not found: {env_path}")
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == "token" and value.strip():
            return value.strip().strip("'\"")
    raise ValueError("GITHUB_TOKEN is missing and github.env has no token value")

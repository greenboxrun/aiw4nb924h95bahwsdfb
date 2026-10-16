"""Small filesystem helpers shared by command-line crawlers."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


# Windows may briefly lock the destination (antivirus, indexers); back off and retry.
REPLACE_ATTEMPTS = 10
REPLACE_BACKOFF_SECONDS = 0.25


def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Write text through a sibling temporary file and replace the destination."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding=encoding,
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(content)
            temporary_path = Path(temporary.name)
        _replace_with_retry(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _replace_with_retry(source: Path, destination: Path) -> None:
    for attempt in range(1, REPLACE_ATTEMPTS + 1):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS:
                raise
            time.sleep(REPLACE_BACKOFF_SECONDS * attempt)


def atomic_write_json(path: Path, payload: Any) -> None:
    """Write pretty UTF-8 JSON atomically so readers never see a partial file."""
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

"""Small filesystem helpers shared by command-line crawlers."""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path


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
        last_error: PermissionError | None = None
        for attempt in range(10):
            try:
                os.replace(temporary_path, path)
                temporary_path = None
                break
            except PermissionError as error:
                last_error = error
                if attempt < 9:
                    time.sleep(0.25 * (attempt + 1))
        if temporary_path is not None:
            raise last_error or PermissionError(f"출력 파일 교체 실패: {path}")
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

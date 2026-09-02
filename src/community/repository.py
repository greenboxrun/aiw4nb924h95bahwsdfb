"""Atomic JSON persistence for community crawl records."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class JsonRecordRepository:
    """Load and atomically replace the fixed community result snapshot."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"기존 JSON을 읽을 수 없습니다: {self.path} ({error})") from error
        if not isinstance(payload, list):
            raise ValueError(f"JSON 최상위 값은 배열이어야 합니다: {self.path}")
        return [row for row in payload if isinstance(row, dict)]

    def save(self, records: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        temporary.write_text(
            json.dumps(records, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        last_error: PermissionError | None = None
        for attempt in range(10):
            try:
                temporary.replace(self.path)
                return
            except PermissionError as error:
                last_error = error
                time.sleep(0.25 * (attempt + 1))
        raise last_error or PermissionError(f"출력 파일 교체 실패: {self.path}")

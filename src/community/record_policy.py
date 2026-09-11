"""Pure policies for normalizing and maintaining crawl records."""

from __future__ import annotations

from typing import Any

from .models import OUTPUT_FIELDS, ISSUELINK_SOURCE, SOURCE_FIELD, ListingCandidate
from .parsing import record_key


Record = dict[str, Any]
RecordKey = tuple[str, str]


def build_record_cache(records: list[Record]) -> dict[RecordKey, Record]:
    """Index valid records by their source-site/post-ID key."""
    cache: dict[RecordKey, Record] = {}
    for record in records:
        key = record_key(record)
        if key is not None:
            cache[key] = record
    return cache


def build_original_url_cache(records: list[Record]) -> dict[RecordKey, str]:
    """Index reusable original URLs without changing the stored records."""
    cache: dict[RecordKey, str] = {}
    for record in records:
        key = record_key(record)
        original_url = str(record.get("원문URL", "")).strip()
        if key is not None and original_url:
            cache[key] = original_url
    return cache


def merge_source_scores(previous_sources: object, score: int) -> list[dict[str, object]]:
    """Replace this crawler's score while retaining other collection sources."""
    merged: list[dict[str, object]] = []
    if isinstance(previous_sources, list):
        for source in previous_sources:
            if not isinstance(source, dict):
                continue
            if source.get("source") == ISSUELINK_SOURCE:
                continue
            merged.append(dict(source))
    merged.append({"source": ISSUELINK_SOURCE, "score": score})
    return merged


class SnapshotBuilder:
    """Build one fresh snapshot while retaining compatible cached metadata."""

    def __init__(self, cached_records: list[Record]) -> None:
        self._cached_records = build_record_cache(cached_records)
        self._original_url_cache = build_original_url_cache(cached_records)
        self._records: list[Record] = []
        self._known_keys: set[RecordKey] = set()

    @property
    def records(self) -> list[Record]:
        return self._records

    @property
    def size(self) -> int:
        return len(self._records)

    @property
    def cached_url_count(self) -> int:
        return len(self._original_url_cache)

    def contains(self, key: RecordKey) -> bool:
        return key in self._known_keys

    def cached_original_url(self, key: RecordKey) -> str | None:
        return self._original_url_cache.get(key)

    def add(
        self,
        candidate: ListingCandidate,
        original_url: str,
        score: int,
    ) -> Record:
        if self.contains(candidate.key):
            raise ValueError(f"duplicate snapshot record: {candidate.key}")

        cached_record = self._cached_records.get(candidate.key)
        sources = merge_source_scores(
            cached_record.get(SOURCE_FIELD) if cached_record else None,
            score,
        )
        record = candidate.to_record(original_url, score, sources)
        if cached_record:
            for field, value in cached_record.items():
                if field not in OUTPUT_FIELDS:
                    record[field] = value
        self._records.append(record)
        self._known_keys.add(candidate.key)
        return record

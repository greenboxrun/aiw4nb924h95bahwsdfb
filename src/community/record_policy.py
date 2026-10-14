"""Pure policies for normalizing and maintaining crawl records."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .models import (
    ISSUELINK_SOURCES,
    ISSUELINK_SOURCE_ORDER,
    OUTPUT_FIELDS,
    SOURCE_FIELD,
    ListingCandidate,
)
from .parsing import is_expired, record_key


Record = dict[str, Any]
RecordKey = tuple[str, str]


@dataclass(frozen=True, slots=True)
class CarryOverCandidate:
    """A previous record kept because its listing source failed this run."""

    record: Record
    score: int


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


def source_scores_of(record: Mapping[str, Any]) -> dict[str, int]:
    """Read the IssueLink list ranks stored in a record's collection sources."""
    scores: dict[str, int] = {}
    sources = record.get(SOURCE_FIELD)
    if not isinstance(sources, list):
        return scores
    for source in sources:
        if not isinstance(source, dict):
            continue
        name = source.get("source")
        score = source.get("score")
        if (
            isinstance(name, str)
            and name in ISSUELINK_SOURCE_ORDER
            and isinstance(score, int)
            and not isinstance(score, bool)
        ):
            scores.setdefault(name, score)
    return scores


def merge_source_scores(
    previous_sources: object,
    source_scores: Mapping[str, int],
    retained_sources: Collection[str] = (),
) -> list[dict[str, object]]:
    """Replace IssueLink scores while retaining other collection sources.

    Scores of ``retained_sources`` (lists that failed to load this run) keep
    their previous value unless this run collected a fresh score for them.
    """
    merged: list[dict[str, object]] = []
    previous_scores: dict[str, int] = {}
    if isinstance(previous_sources, list):
        previous_scores = source_scores_of({SOURCE_FIELD: previous_sources})
        for source in previous_sources:
            if not isinstance(source, dict):
                continue
            if (
                isinstance(source.get("source"), str)
                and source.get("source") in ISSUELINK_SOURCES
            ):
                continue
            merged.append(dict(source))
    for source in ISSUELINK_SOURCE_ORDER:
        score = source_scores.get(source)
        if score is None and source in retained_sources:
            score = previous_scores.get(source)
        if score is not None:
            merged.append({"source": source, "score": score})
    return merged


def select_carry_over(
    cached_records: Iterable[Record],
    failed_sources: Collection[str],
    fresh_keys: Collection[RecordKey],
    retention_hours: int,
    now: datetime,
) -> list[CarryOverCandidate]:
    """Pick previous records from failed lists that this run could not refresh."""
    if not failed_sources:
        return []
    selected: list[CarryOverCandidate] = []
    seen: set[RecordKey] = set()
    for record in cached_records:
        key = record_key(record)
        if key is None or key in fresh_keys or key in seen:
            continue
        failed_scores = [
            score
            for source, score in source_scores_of(record).items()
            if source in failed_sources
        ]
        if not failed_scores or is_expired(record, retention_hours, now):
            continue
        seen.add(key)
        selected.append(CarryOverCandidate(dict(record), min(failed_scores)))
    return selected


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

    def cached_source_scores(self, key: RecordKey) -> dict[str, int]:
        cached_record = self._cached_records.get(key)
        return source_scores_of(cached_record) if cached_record else {}

    def carry_over_candidates(
        self,
        failed_sources: Collection[str],
        fresh_keys: Collection[RecordKey],
        retention_hours: int,
        now: datetime,
    ) -> list[CarryOverCandidate]:
        return select_carry_over(
            self._cached_records.values(),
            failed_sources,
            fresh_keys,
            retention_hours,
            now,
        )

    def add(
        self,
        candidate: ListingCandidate,
        original_url: str,
        source_scores: Mapping[str, int],
        retained_sources: Collection[str] = (),
    ) -> Record:
        if self.contains(candidate.key):
            raise ValueError(f"duplicate snapshot record: {candidate.key}")

        cached_record = self._cached_records.get(candidate.key)
        sources = merge_source_scores(
            cached_record.get(SOURCE_FIELD) if cached_record else None,
            source_scores,
            retained_sources,
        )
        record = candidate.to_record(
            original_url,
            min(source_scores.values()),
            sources,
        )
        if cached_record:
            for field, value in cached_record.items():
                if field not in OUTPUT_FIELDS:
                    record[field] = value
        self._records.append(record)
        self._known_keys.add(candidate.key)
        return record

    def add_carried(self, record: Record) -> Record:
        key = record_key(record)
        if key is None:
            raise ValueError("carried snapshot record has no key")
        if self.contains(key):
            raise ValueError(f"duplicate snapshot record: {key}")
        self._records.append(record)
        self._known_keys.add(key)
        return record

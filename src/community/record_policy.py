"""Pure policies for normalizing and maintaining crawl records."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .models import ListingCandidate
from .parsing import parse_integer, parse_written_at, record_key


Record = dict[str, Any]
RecordKey = tuple[str, str]


def deduplicate(records: list[Record]) -> list[Record]:
    """Keep the last stored copy for each source-site/post-ID pair."""
    unique_records: list[Record] = []
    indexes: dict[RecordKey, int] = {}
    for record in records:
        key = record_key(record)
        if key is None:
            unique_records.append(record)
        elif key in indexes:
            unique_records[indexes[key]] = record
        else:
            indexes[key] = len(unique_records)
            unique_records.append(record)
    return unique_records


def build_indexes(records: list[Record]) -> tuple[set[RecordKey], dict[RecordKey, int]]:
    """Build lookup structures used while merging listing candidates."""
    indexes = {
        key: index
        for index, record in enumerate(records)
        if (key := record_key(record)) is not None
    }
    return set(indexes), indexes


def limit_to_target(records: list[Record], target: int) -> None:
    """Keep highest-view records, preferring newer records on ties."""
    records.sort(key=record_sort_key)
    del records[target:]


def update_existing(record: Record, candidate: ListingCandidate) -> bool:
    """Apply listing metadata to an existing record and report changes."""
    values = {
        "제목": candidate.title,
        "작성시간": candidate.written_at,
        "댓글수": candidate.comment_count,
        "조회수": candidate.view_count,
    }
    changed = False
    for field, value in values.items():
        if record.get(field) != value:
            record[field] = value
            changed = True
    return changed


def record_sort_key(record: Record) -> tuple[int, int, str, str]:
    written_at = parse_written_at(str(record.get("작성시간", "")))
    return (
        -parse_integer(str(record.get("조회수", ""))),
        -datetime_sort_value(written_at),
        str(record.get("사이트", "")),
        str(record.get("id값", "")),
    )


def datetime_sort_value(value: datetime | None) -> int:
    if value is None:
        return 0
    return (
        value.toordinal() * 86_400
        + value.hour * 3_600
        + value.minute * 60
        + value.second
    )

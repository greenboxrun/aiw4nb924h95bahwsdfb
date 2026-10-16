"""Run the FMKorea list workflow and retrieve its JSON artifact."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.common.github_workflows import (
    WorkflowRequest,
    WORKFLOW_ERRORS,
    emit_json_result,
    run_workflow,
)

WORKFLOW_FILE = "fmkorea-list.yml"
ARTIFACT_NAME = "fmkorea-list"


def validate_pages(value: str) -> str:
    if not value.isdigit() or not 1 <= int(value) <= 6:
        raise ValueError("pages must be an integer from 1 through 6")
    return str(int(value))


def main() -> int:
    parser = argparse.ArgumentParser(description="Request an FMKorea list crawl and download the JSON artifact")
    parser.add_argument("--pages", default="6", help="Number of list pages to crawl (1-6, default: 6)")
    parser.add_argument("-o", "--output", type=Path, help="Optional path to save result.json")
    args = parser.parse_args()
    try:
        pages = validate_pages(args.pages)
        result = run_workflow(
            WorkflowRequest(
                workflow_file=WORKFLOW_FILE,
                artifact_name=ARTIFACT_NAME,
                inputs={"pages": pages},
                completion_timeout_seconds=900,
            )
        )
        emit_json_result(result, args.output)
        return 0
    except WORKFLOW_ERRORS as error:
        print(f"FMKorea list request failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

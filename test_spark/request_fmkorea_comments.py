"""Run the FMKorea comments workflow and retrieve its JSON artifact."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.common.github_workflows import (
    WorkflowRequest,
    WORKFLOW_ERRORS,
    run_workflow,
    save_json_result,
)

WORKFLOW_FILE = "fmkorea-comments.yml"
ARTIFACT_PREFIX = "fmkorea-comments-"


def validate_post_id(value: str) -> str:
    if not value.isdigit() or int(value) <= 0:
        raise GitHubError("post_id must be a positive integer")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Request FMKorea comments and download the JSON artifact")
    parser.add_argument("--post-id", required=True)
    parser.add_argument("-o", "--output", type=Path, help="Optional path to save result.json")
    args = parser.parse_args()
    try:
        post_id = validate_post_id(args.post_id)
        result = run_workflow(
            WorkflowRequest(
                workflow_file=WORKFLOW_FILE,
                artifact_name=f"{ARTIFACT_PREFIX}{post_id}",
                inputs={"post_id": post_id},
                completion_timeout_seconds=600,
            )
        )
        if args.output:
            save_json_result(result, args.output)
            print(f"saved result to {args.output}")
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except WORKFLOW_ERRORS as error:
        print(f"FMKorea comment request failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

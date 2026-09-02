"""Run the FMKorea comments workflow and retrieve its JSON artifact."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests

from src.common.config import load_token
from src.common.github_actions import GitHubError, create_session, dispatch_workflow, download_result, list_runs, wait_for_completion, wait_for_new_run

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
        session = create_session(load_token())
        known_ids = {run["id"] for run in list_runs(session, WORKFLOW_FILE) if isinstance(run.get("id"), int)}
        dispatch_workflow(session, WORKFLOW_FILE, {"post_id": post_id})
        run = wait_for_new_run(session, WORKFLOW_FILE, known_ids, 45, 5)
        completed = wait_for_completion(session, run, 600, 5)
        run_id = completed.get("id")
        if not isinstance(run_id, int):
            raise GitHubError("Completed workflow run did not contain a valid run ID")
        result = download_result(session, run_id, f"{ARTIFACT_PREFIX}{post_id}")
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"saved result to {args.output}")
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (GitHubError, requests.RequestException, ValueError) as error:
        print(f"FMKorea comment request failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

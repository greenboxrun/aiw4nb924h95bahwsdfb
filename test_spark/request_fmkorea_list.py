"""Run the FMKorea list workflow and retrieve its JSON artifact."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests

from src.common.config import load_token
from src.common.github_actions import GitHubError, create_session, dispatch_workflow, download_result, list_runs, wait_for_completion, wait_for_new_run

WORKFLOW_FILE = "fmkorea-list.yml"
ARTIFACT_NAME = "fmkorea-list"


def validate_pages(value: str) -> str:
    if not value.isdigit() or not 1 <= int(value) <= 6:
        raise GitHubError("pages must be an integer from 1 through 6")
    return str(int(value))


def main() -> int:
    parser = argparse.ArgumentParser(description="Request an FMKorea list crawl and download the JSON artifact")
    parser.add_argument("--pages", default="6", help="Number of list pages to crawl (1-6, default: 6)")
    parser.add_argument("-o", "--output", type=Path, help="Optional path to save result.json")
    args = parser.parse_args()
    try:
        pages = validate_pages(args.pages)
        session = create_session(load_token())
        known_ids = {run["id"] for run in list_runs(session, WORKFLOW_FILE) if isinstance(run.get("id"), int)}
        dispatch_workflow(session, WORKFLOW_FILE, {"pages": pages})
        run = wait_for_new_run(session, WORKFLOW_FILE, known_ids, 45, 5)
        completed = wait_for_completion(session, run, 900, 5)
        run_id = completed.get("id")
        if not isinstance(run_id, int):
            raise GitHubError("Completed workflow run did not contain a valid run ID")
        result = download_result(session, run_id, ARTIFACT_NAME)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"saved result to {args.output}")
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (GitHubError, requests.RequestException, ValueError) as error:
        print(f"FMKorea list request failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

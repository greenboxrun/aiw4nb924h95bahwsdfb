"""Run the hello GitHub Actions workflow and print its JSON artifact."""

from __future__ import annotations

import json
import argparse
from datetime import datetime, timezone

import requests

from src.common.config import load_token
from src.common.github_actions import GitHubError, create_session, dispatch_workflow, download_result, list_runs, wait_for_completion, wait_for_new_run

WORKFLOW_FILE = "hello-artifact.yml"
ARTIFACT_NAME = "hello-result"


def main() -> int:
    argparse.ArgumentParser(description="Run the hello GitHub Actions workflow and print its artifact").parse_args()
    try:
        session = create_session(load_token(), "hello-artifact-client")
        known_ids = {run["id"] for run in list_runs(session, WORKFLOW_FILE) if isinstance(run.get("id"), int)}
        request_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        dispatch_workflow(session, WORKFLOW_FILE, {"request_id": request_id})
        run = wait_for_new_run(session, WORKFLOW_FILE, known_ids, 30, 3)
        completed = wait_for_completion(session, run, 180, 3)
        run_id = completed.get("id")
        if not isinstance(run_id, int):
            raise GitHubError("Completed workflow run did not contain a valid run ID")
        print(json.dumps(download_result(session, run_id, ARTIFACT_NAME), ensure_ascii=False, indent=2))
        return 0
    except (GitHubError, requests.RequestException, ValueError) as error:
        print(f"hello artifact test failed: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

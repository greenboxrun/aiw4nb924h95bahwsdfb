"""Run the hello GitHub Actions workflow and print its JSON artifact."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from src.common.github_workflows import WORKFLOW_ERRORS, WorkflowRequest, run_workflow

WORKFLOW_FILE = "hello-artifact.yml"
ARTIFACT_NAME = "hello-result"


def main() -> int:
    argparse.ArgumentParser(description="Run the hello GitHub Actions workflow and print its artifact").parse_args()
    try:
        request_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        result = run_workflow(
            WorkflowRequest(
                workflow_file=WORKFLOW_FILE,
                artifact_name=ARTIFACT_NAME,
                inputs={"request_id": request_id},
                new_run_timeout_seconds=30,
                completion_timeout_seconds=180,
                poll_interval_seconds=3,
                user_agent="hello-artifact-client",
            )
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except WORKFLOW_ERRORS as error:
        print(f"hello artifact test failed: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Application service for dispatching GitHub Actions workflows."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from .config import load_token
from .files import atomic_write_text
from .github_actions import (
    GitHubError,
    create_session,
    dispatch_workflow,
    download_result,
    list_runs,
    wait_for_completion,
    wait_for_new_run,
)


@dataclass(frozen=True, slots=True)
class WorkflowRequest:
    """All workflow-specific values needed for one request."""

    workflow_file: str
    artifact_name: str
    inputs: dict[str, str]
    new_run_timeout_seconds: int = 45
    completion_timeout_seconds: int = 600
    poll_interval_seconds: int = 5
    user_agent: str = "fmkorea-crawler"


class GitHubWorkflowRunner:
    """Run a dispatched workflow and return its JSON artifact."""

    def __init__(self, token: str, *, user_agent: str = "fmkorea-crawler") -> None:
        self._session = create_session(token, user_agent)

    def run(self, request: WorkflowRequest) -> dict[str, Any]:
        known_run_ids = self._known_run_ids(request.workflow_file)
        dispatch_workflow(
            self._session,
            request.workflow_file,
            request.inputs,
        )
        run = wait_for_new_run(
            self._session,
            request.workflow_file,
            known_run_ids,
            request.new_run_timeout_seconds,
            request.poll_interval_seconds,
        )
        completed = wait_for_completion(
            self._session,
            run,
            request.completion_timeout_seconds,
            request.poll_interval_seconds,
        )
        run_id = completed.get("id")
        if not isinstance(run_id, int):
            raise GitHubError("Completed workflow run did not contain a valid run ID")
        return download_result(
            self._session,
            run_id,
            request.artifact_name,
        )

    def _known_run_ids(self, workflow_file: str) -> set[int]:
        return {
            run_id
            for run in list_runs(self._session, workflow_file)
            if isinstance(run_id := run.get("id"), int)
        }


def run_workflow(request: WorkflowRequest) -> dict[str, Any]:
    """Run a workflow using the repository's configured GitHub token."""
    return GitHubWorkflowRunner(
        load_token(),
        user_agent=request.user_agent,
    ).run(request)


def save_json_result(result: dict[str, Any], output: Path) -> None:
    """Persist a workflow result without exposing a partially written JSON file."""
    atomic_write_text(
        output,
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
    )


WORKFLOW_ERRORS = (GitHubError, requests.RequestException, ValueError)

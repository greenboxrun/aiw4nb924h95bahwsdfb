"""Application service for dispatching GitHub Actions workflows."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from .config import load_token
from .files import atomic_write_json
from .github_actions import GitHubActionsClient, GitHubError, create_session


DEFAULT_USER_AGENT = "fmkorea-crawler"


@dataclass(frozen=True, slots=True)
class WorkflowRequest:
    """All workflow-specific values needed for one request."""

    workflow_file: str
    artifact_name: str
    inputs: dict[str, str]
    new_run_timeout_seconds: int = 45
    completion_timeout_seconds: int = 600
    poll_interval_seconds: int = 5
    user_agent: str = DEFAULT_USER_AGENT


class GitHubWorkflowRunner:
    """Run a dispatched workflow and return its JSON artifact."""

    def __init__(self, client: GitHubActionsClient) -> None:
        self._client = client

    def run(self, request: WorkflowRequest) -> dict[str, Any]:
        known_run_ids = self._client.known_run_ids(request.workflow_file)
        self._client.dispatch(request.workflow_file, request.inputs)
        run = self._client.wait_for_new_run(
            request.workflow_file,
            known_run_ids,
            request.new_run_timeout_seconds,
            request.poll_interval_seconds,
        )
        completed = self._client.wait_for_success(
            run,
            request.completion_timeout_seconds,
            request.poll_interval_seconds,
        )
        return self._client.download_result(completed, request.artifact_name)


def run_workflow(request: WorkflowRequest) -> dict[str, Any]:
    """Run a workflow using the repository's configured GitHub token."""
    session = create_session(load_token(), request.user_agent)
    return GitHubWorkflowRunner(GitHubActionsClient(session)).run(request)


def save_json_result(result: dict[str, Any], output: Path) -> None:
    """Persist a workflow result without exposing a partially written JSON file."""
    atomic_write_json(output, result)


def emit_json_result(result: dict[str, Any], output: Path | None) -> None:
    """Save the result when an output path is given, otherwise print it."""
    if output is None:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    save_json_result(result, output)
    print(f"saved result to {output}")


WORKFLOW_ERRORS = (GitHubError, requests.RequestException, ValueError)

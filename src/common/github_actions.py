"""GitHub Actions REST client scoped to one repository."""

from __future__ import annotations

import json
import time
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

import requests

from .config import API_BASE, API_VERSION, OWNER, REF, REPOSITORY


REQUEST_TIMEOUT_SECONDS = 30
DOWNLOAD_TIMEOUT_SECONDS = 60
ERROR_DETAIL_CHARS = 500
RESULT_FILE_NAME = "result.json"


class GitHubError(RuntimeError):
    """Raised when GitHub returns an unexpected response or run state."""


def create_session(token: str, user_agent: str = "fmkorea-crawler") -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": user_agent,
    })
    return session


def _error_detail(response: requests.Response) -> str:
    return response.text[:ERROR_DETAIL_CHARS].replace("\n", " ")


def _run_id(run: dict[str, Any]) -> int:
    run_id = run.get("id")
    if not isinstance(run_id, int):
        raise GitHubError("The workflow run did not contain a valid run ID")
    return run_id


class GitHubActionsClient:
    """Dispatch workflows, follow their runs and download their artifacts."""

    def __init__(
        self,
        session: requests.Session,
        *,
        owner: str = OWNER,
        repository: str = REPOSITORY,
        ref: str = REF,
    ) -> None:
        self._session = session
        self._ref = ref
        self._repo_url = f"{API_BASE}/repos/{owner}/{repository}"

    def list_runs(self, workflow_file: str) -> list[dict[str, Any]]:
        payload = self._request_json(
            "GET",
            f"{self._workflow_url(workflow_file)}/runs",
            params={"branch": self._ref, "event": "workflow_dispatch", "per_page": 20},
        )
        runs = payload.get("workflow_runs", [])
        if not isinstance(runs, list):
            raise GitHubError("GitHub returned an invalid workflow_runs response")
        return [run for run in runs if isinstance(run, dict)]

    def known_run_ids(self, workflow_file: str) -> set[int]:
        return {
            run_id
            for run in self.list_runs(workflow_file)
            if isinstance(run_id := run.get("id"), int)
        }

    def dispatch(self, workflow_file: str, inputs: dict[str, str]) -> None:
        response = self._session.post(
            f"{self._workflow_url(workflow_file)}/dispatches",
            json={"ref": self._ref, "inputs": inputs},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code != 204:
            raise GitHubError(
                f"Workflow dispatch failed with status {response.status_code}: {_error_detail(response)}"
            )

    def wait_for_new_run(
        self,
        workflow_file: str,
        known_run_ids: set[int],
        timeout_seconds: int,
        poll_interval_seconds: int,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            for run in self.list_runs(workflow_file):
                run_id = run.get("id")
                if isinstance(run_id, int) and run_id not in known_run_ids:
                    return run
            time.sleep(poll_interval_seconds)
        raise GitHubError("A workflow run was not created before the timeout")

    def wait_for_success(
        self,
        run: dict[str, Any],
        timeout_seconds: int,
        poll_interval_seconds: int,
    ) -> dict[str, Any]:
        run_id = _run_id(run)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            current = self._request_json("GET", f"{self._repo_url}/actions/runs/{run_id}")
            status = current.get("status")
            print(f"workflow run {run_id}: {status or 'unknown'}")
            if status == "completed":
                if current.get("conclusion") != "success":
                    raise GitHubError(f"Workflow completed with conclusion: {current.get('conclusion')}")
                return current
            time.sleep(poll_interval_seconds)
        raise GitHubError("The workflow did not complete before the timeout")

    def download_result(self, run: dict[str, Any], artifact_name: str) -> dict[str, Any]:
        """Download an artifact ZIP and return the result.json object inside it."""
        artifact_id = self._find_artifact_id(_run_id(run), artifact_name)
        response = self._session.get(
            f"{self._repo_url}/actions/artifacts/{artifact_id}/zip",
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
        )
        if not response.ok:
            raise GitHubError(f"Artifact download failed with {response.status_code}")
        return _read_result_json(response.content)

    def _find_artifact_id(self, run_id: int, artifact_name: str) -> int:
        payload = self._request_json(
            "GET",
            f"{self._repo_url}/actions/runs/{run_id}/artifacts",
            params={"per_page": 100},
        )
        artifact = next(
            (
                item
                for item in payload.get("artifacts", [])
                if isinstance(item, dict) and item.get("name") == artifact_name
            ),
            None,
        )
        if artifact is None:
            raise GitHubError(f"Artifact {artifact_name!r} was not found")
        if artifact.get("expired") is True:
            raise GitHubError(f"Artifact {artifact_name!r} has expired")
        artifact_id = artifact.get("id")
        if not isinstance(artifact_id, int):
            raise GitHubError("The artifact did not contain a valid ID")
        return artifact_id

    def _workflow_url(self, workflow_file: str) -> str:
        return f"{self._repo_url}/actions/workflows/{workflow_file}"

    def _request_json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        response = self._session.request(method, url, timeout=REQUEST_TIMEOUT_SECONDS, **kwargs)
        if not response.ok:
            raise GitHubError(
                f"GitHub API {method} {url} failed with {response.status_code}: {_error_detail(response)}"
            )
        if not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError as error:
            raise GitHubError(f"GitHub returned invalid JSON from {url}") from error
        if not isinstance(payload, dict):
            raise GitHubError(f"GitHub returned an unexpected response from {url}")
        return payload


def _read_result_json(archive_bytes: bytes) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(BytesIO(archive_bytes)) as archive:
            result_name = next(
                (name for name in archive.namelist() if Path(name).name == RESULT_FILE_NAME),
                None,
            )
            if result_name is None:
                raise GitHubError(f"The artifact ZIP does not contain {RESULT_FILE_NAME}")
            result = json.loads(archive.read(result_name).decode("utf-8"))
    except (zipfile.BadZipFile, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GitHubError("The artifact did not contain valid JSON") from error
    if not isinstance(result, dict):
        raise GitHubError(f"{RESULT_FILE_NAME} must contain a JSON object")
    return result

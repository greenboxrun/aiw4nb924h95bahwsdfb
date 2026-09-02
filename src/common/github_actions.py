"""Small GitHub Actions API helpers shared by request CLIs."""

from __future__ import annotations

import json
import time
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

import requests

from .config import API_BASE, API_VERSION, OWNER, REF, REPOSITORY


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


def request_json(session: requests.Session, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    response = session.request(method, url, timeout=30, **kwargs)
    if not response.ok:
        detail = response.text[:500].replace("\n", " ")
        raise GitHubError(f"GitHub API {method} {url} failed with {response.status_code}: {detail}")
    if not response.content:
        return {}
    try:
        payload = response.json()
    except ValueError as error:
        raise GitHubError(f"GitHub returned invalid JSON from {url}") from error
    if not isinstance(payload, dict):
        raise GitHubError(f"GitHub returned an unexpected response from {url}")
    return payload


def workflow_url(workflow_file: str) -> str:
    return f"{API_BASE}/repos/{OWNER}/{REPOSITORY}/actions/workflows/{workflow_file}"


def list_runs(session: requests.Session, workflow_file: str) -> list[dict[str, Any]]:
    payload = request_json(session, "GET", f"{workflow_url(workflow_file)}/runs", params={"branch": REF, "event": "workflow_dispatch", "per_page": 20})
    runs = payload.get("workflow_runs", [])
    if not isinstance(runs, list):
        raise GitHubError("GitHub returned an invalid workflow_runs response")
    return [run for run in runs if isinstance(run, dict)]


def dispatch_workflow(session: requests.Session, workflow_file: str, inputs: dict[str, str]) -> None:
    response = session.post(f"{workflow_url(workflow_file)}/dispatches", json={"ref": REF, "inputs": inputs}, timeout=30)
    if response.status_code != 204:
        detail = response.text[:500].replace("\n", " ")
        raise GitHubError(f"Workflow dispatch failed with status {response.status_code}: {detail}")


def wait_for_new_run(session: requests.Session, workflow_file: str, known_run_ids: set[int], timeout_seconds: int, poll_interval_seconds: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        for run in list_runs(session, workflow_file):
            run_id = run.get("id")
            if isinstance(run_id, int) and run_id not in known_run_ids:
                return run
        time.sleep(poll_interval_seconds)
    raise GitHubError("A workflow run was not created before the timeout")


def wait_for_completion(session: requests.Session, run: dict[str, Any], timeout_seconds: int, poll_interval_seconds: int) -> dict[str, Any]:
    run_id = run.get("id")
    if not isinstance(run_id, int):
        raise GitHubError("The workflow run did not contain a valid run ID")
    deadline = time.monotonic() + timeout_seconds
    run_url = f"{API_BASE}/repos/{OWNER}/{REPOSITORY}/actions/runs/{run_id}"
    while time.monotonic() < deadline:
        current = request_json(session, "GET", run_url)
        status = current.get("status")
        print(f"workflow run {run_id}: {status or 'unknown'}")
        if status == "completed":
            if current.get("conclusion") != "success":
                raise GitHubError(f"Workflow completed with conclusion: {current.get('conclusion')}")
            return current
        time.sleep(poll_interval_seconds)
    raise GitHubError("The workflow did not complete before the timeout")


def download_result(session: requests.Session, run_id: int, artifact_name: str) -> dict[str, Any]:
    payload = request_json(session, "GET", f"{API_BASE}/repos/{OWNER}/{REPOSITORY}/actions/runs/{run_id}/artifacts", params={"per_page": 100})
    artifacts = payload.get("artifacts", [])
    artifact = next((item for item in artifacts if isinstance(item, dict) and item.get("name") == artifact_name), None)
    if not isinstance(artifact, dict):
        raise GitHubError(f"Artifact {artifact_name!r} was not found")
    if artifact.get("expired") is True:
        raise GitHubError(f"Artifact {artifact_name!r} has expired")
    artifact_id = artifact.get("id")
    if not isinstance(artifact_id, int):
        raise GitHubError("The artifact did not contain a valid ID")
    response = session.get(f"{API_BASE}/repos/{OWNER}/{REPOSITORY}/actions/artifacts/{artifact_id}/zip", timeout=60)
    if not response.ok:
        raise GitHubError(f"Artifact download failed with {response.status_code}")
    try:
        with zipfile.ZipFile(BytesIO(response.content)) as archive:
            result_name = next((name for name in archive.namelist() if Path(name).name == "result.json"), None)
            if result_name is None:
                raise GitHubError("The artifact ZIP does not contain result.json")
            result = json.loads(archive.read(result_name).decode("utf-8"))
    except (zipfile.BadZipFile, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GitHubError("The artifact did not contain valid JSON") from error
    if not isinstance(result, dict):
        raise GitHubError("result.json must contain a JSON object")
    return result

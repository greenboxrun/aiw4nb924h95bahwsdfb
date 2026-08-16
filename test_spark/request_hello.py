"""Run the hello GitHub Actions workflow and print its JSON artifact."""

from __future__ import annotations

import json
import sys
import time
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

import requests


OWNER = "greenboxrun"
REPOSITORY = "aiw4nb924h95bahwsdfb"
WORKFLOW_FILE = "hello-artifact.yml"
REF = "master"
ARTIFACT_NAME = "hello-result"
API_BASE = "https://api.github.com"
API_VERSION = "2022-11-28"
POLL_INTERVAL_SECONDS = 3
DISPATCH_TIMEOUT_SECONDS = 30
RUN_TIMEOUT_SECONDS = 180


class GitHubError(RuntimeError):
    """Raised when GitHub returns an unexpected response."""


def load_token() -> str:
    env_path = Path(__file__).resolve().parent.parent / "github.env"
    if not env_path.is_file():
        raise GitHubError(f"Token file not found: {env_path}")

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == "token" and value.strip():
            return value.strip().strip('"').strip("'")

    raise GitHubError("The token key is missing or empty in github.env")


def create_session(token: str) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "hello-artifact-test",
        }
    )
    return session


def request_json(
    session: requests.Session,
    method: str,
    url: str,
    **kwargs: Any,
) -> dict[str, Any]:
    response = session.request(method, url, timeout=30, **kwargs)
    if not response.ok:
        detail = response.text[:500].replace("\n", " ")
        raise GitHubError(
            f"GitHub API {method} {url} failed with {response.status_code}: {detail}"
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


def workflow_url() -> str:
    return f"{API_BASE}/repos/{OWNER}/{REPOSITORY}/actions/workflows/{WORKFLOW_FILE}"


def list_runs(session: requests.Session) -> list[dict[str, Any]]:
    payload = request_json(
        session,
        "GET",
        f"{workflow_url()}/runs",
        params={"branch": REF, "event": "workflow_dispatch", "per_page": 20},
    )
    runs = payload.get("workflow_runs", [])
    if not isinstance(runs, list):
        raise GitHubError("GitHub returned an invalid workflow_runs response")
    return [run for run in runs if isinstance(run, dict)]


def dispatch_workflow(session: requests.Session, request_id: str) -> None:
    response = session.post(
        f"{workflow_url()}/dispatches",
        json={"ref": REF, "inputs": {"request_id": request_id}},
        timeout=30,
    )
    if response.status_code != 204:
        detail = response.text[:500].replace("\n", " ")
        raise GitHubError(
            f"Workflow dispatch failed with {response.status_code}: {detail}"
        )


def wait_for_new_run(
    session: requests.Session,
    known_run_ids: set[int],
) -> dict[str, Any]:
    deadline = time.monotonic() + DISPATCH_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        for run in list_runs(session):
            run_id = run.get("id")
            if isinstance(run_id, int) and run_id not in known_run_ids:
                return run
        time.sleep(POLL_INTERVAL_SECONDS)
    raise GitHubError("A workflow run was not created before the timeout")


def wait_for_completion(
    session: requests.Session,
    run: dict[str, Any],
) -> dict[str, Any]:
    run_id = run.get("id")
    if not isinstance(run_id, int):
        raise GitHubError("The workflow run did not contain a valid run ID")

    deadline = time.monotonic() + RUN_TIMEOUT_SECONDS
    run_url = f"{API_BASE}/repos/{OWNER}/{REPOSITORY}/actions/runs/{run_id}"
    while time.monotonic() < deadline:
        current = request_json(session, "GET", run_url)
        status = current.get("status")
        conclusion = current.get("conclusion")
        print(f"workflow run {run_id}: {status or 'unknown'}", file=sys.stderr)
        if status == "completed":
            if conclusion != "success":
                raise GitHubError(f"Workflow completed with conclusion: {conclusion}")
            return current
        time.sleep(POLL_INTERVAL_SECONDS)
    raise GitHubError("The workflow did not complete before the timeout")


def download_result(session: requests.Session, run_id: int) -> dict[str, Any]:
    artifacts_url = (
        f"{API_BASE}/repos/{OWNER}/{REPOSITORY}/actions/runs/{run_id}/artifacts"
    )
    payload = request_json(session, "GET", artifacts_url, params={"per_page": 100})
    artifacts = payload.get("artifacts", [])
    artifact = next(
        (
            item
            for item in artifacts
            if isinstance(item, dict) and item.get("name") == ARTIFACT_NAME
        ),
        None,
    )
    if not isinstance(artifact, dict):
        raise GitHubError(f"Artifact {ARTIFACT_NAME!r} was not found")
    if artifact.get("expired") is True:
        raise GitHubError(f"Artifact {ARTIFACT_NAME!r} has expired")

    artifact_id = artifact.get("id")
    if not isinstance(artifact_id, int):
        raise GitHubError("The artifact did not contain a valid ID")

    download_url = f"{API_BASE}/repos/{OWNER}/{REPOSITORY}/actions/artifacts/{artifact_id}/zip"
    response = session.get(download_url, timeout=30)
    if not response.ok:
        detail = response.text[:500].replace("\n", " ")
        raise GitHubError(
            f"Artifact download failed with {response.status_code}: {detail}"
        )

    try:
        with zipfile.ZipFile(BytesIO(response.content)) as archive:
            names = archive.namelist()
            result_name = next(
                (name for name in names if Path(name).name == "result.json"), None
            )
            if result_name is None:
                raise GitHubError("The artifact ZIP does not contain result.json")
            result = json.loads(archive.read(result_name).decode("utf-8"))
    except (zipfile.BadZipFile, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GitHubError("The artifact did not contain valid JSON") from error

    if not isinstance(result, dict):
        raise GitHubError("result.json must contain a JSON object")
    return result


def main() -> int:
    try:
        token = load_token()
        session = create_session(token)
        known_run_ids = {
            run_id
            for run in list_runs(session)
            if isinstance((run_id := run.get("id")), int)
        }
        request_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        dispatch_workflow(session, request_id)
        run = wait_for_new_run(session, known_run_ids)
        completed_run = wait_for_completion(session, run)
        run_id = completed_run.get("id")
        if not isinstance(run_id, int):
            raise GitHubError("Completed workflow run did not contain a valid ID")
        result = download_result(session, run_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (GitHubError, requests.RequestException) as error:
        print(f"hello artifact test failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

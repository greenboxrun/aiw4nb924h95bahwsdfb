"""Run the FMKorea comments workflow and retrieve its JSON artifact."""

from __future__ import annotations

import argparse
import json
import sys
import time
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

import requests

from request_hello import GitHubError, create_session, load_token, request_json


OWNER = "greenboxrun"
REPOSITORY = "aiw4nb924h95bahwsdfb"
WORKFLOW_FILE = "fmkorea-comments.yml"
REF = "master"
API_BASE = "https://api.github.com"
ARTIFACT_PREFIX = "fmkorea-comments-"
POLL_INTERVAL_SECONDS = 5
DISPATCH_TIMEOUT_SECONDS = 45
RUN_TIMEOUT_SECONDS = 600


def validate_post_id(value: str) -> str:
    if not value.isdigit() or int(value) <= 0:
        raise GitHubError("post_id must be a positive integer")
    return value


def workflow_url() -> str:
    return f"{API_BASE}/repos/{OWNER}/{REPOSITORY}/actions/workflows/{WORKFLOW_FILE}"


def dispatch_workflow(session: requests.Session, post_id: str) -> None:
    response = session.post(
        f"{workflow_url()}/dispatches",
        json={"ref": REF, "inputs": {"post_id": post_id}},
        timeout=30,
    )
    if response.status_code != 204:
        raise GitHubError(f"Workflow dispatch failed with {response.status_code}: {response.text[:500]}")


def wait_for_new_run(session: requests.Session, known_ids: set[int]) -> dict[str, Any]:
    deadline = time.monotonic() + DISPATCH_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        for run in list_runs_for_workflow(session):
            run_id = run.get("id")
            if isinstance(run_id, int) and run_id not in known_ids:
                return run
        time.sleep(POLL_INTERVAL_SECONDS)
    raise GitHubError("A workflow run was not created before the timeout")


def list_runs_for_workflow(session: requests.Session) -> list[dict[str, Any]]:
    payload = request_json(session, "GET", f"{workflow_url()}/runs", params={"branch": REF, "event": "workflow_dispatch", "per_page": 20})
    runs = payload.get("workflow_runs", [])
    return [run for run in runs if isinstance(run, dict)] if isinstance(runs, list) else []


def wait_for_completion(session: requests.Session, run: dict[str, Any]) -> dict[str, Any]:
    run_id = run.get("id")
    if not isinstance(run_id, int):
        raise GitHubError("Workflow run did not contain a valid ID")
    deadline = time.monotonic() + RUN_TIMEOUT_SECONDS
    url = f"{API_BASE}/repos/{OWNER}/{REPOSITORY}/actions/runs/{run_id}"
    while time.monotonic() < deadline:
        current = request_json(session, "GET", url)
        print(f"workflow run {run_id}: {current.get('status', 'unknown')}", file=sys.stderr)
        if current.get("status") == "completed":
            if current.get("conclusion") != "success":
                raise GitHubError(f"Workflow completed with conclusion: {current.get('conclusion')}")
            return current
        time.sleep(POLL_INTERVAL_SECONDS)
    raise GitHubError("The workflow did not complete before the timeout")


def download_result(session: requests.Session, run_id: int, post_id: str) -> dict[str, Any]:
    url = f"{API_BASE}/repos/{OWNER}/{REPOSITORY}/actions/runs/{run_id}/artifacts"
    payload = request_json(session, "GET", url, params={"per_page": 100})
    expected_name = f"{ARTIFACT_PREFIX}{post_id}"
    artifact = next((item for item in payload.get("artifacts", []) if isinstance(item, dict) and item.get("name") == expected_name), None)
    if not isinstance(artifact, dict):
        raise GitHubError(f"Artifact {expected_name!r} was not found")
    artifact_id = artifact.get("id")
    if not isinstance(artifact_id, int) or artifact.get("expired") is True:
        raise GitHubError("The workflow artifact is invalid or expired")
    response = session.get(f"{API_BASE}/repos/{OWNER}/{REPOSITORY}/actions/artifacts/{artifact_id}/zip", timeout=60)
    if not response.ok:
        raise GitHubError(f"Artifact download failed with {response.status_code}: {response.text[:500]}")
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Request FMKorea comments and download the JSON artifact")
    parser.add_argument("--post-id", required=True)
    parser.add_argument("-o", "--output", type=Path, help="Optional path to save result.json")
    args = parser.parse_args()
    try:
        post_id = validate_post_id(args.post_id)
        session = create_session(load_token())
        known_ids = {run_id for run in list_runs_for_workflow(session) if isinstance((run_id := run.get("id")), int)}
        dispatch_workflow(session, post_id)
        completed = wait_for_completion(session, wait_for_new_run(session, known_ids))
        run_id = completed.get("id")
        if not isinstance(run_id, int):
            raise GitHubError("Completed workflow run did not contain a valid ID")
        result = download_result(session, run_id, post_id)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"saved result to {args.output}")
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (GitHubError, requests.RequestException) as error:
        print(f"FMKorea comment request failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

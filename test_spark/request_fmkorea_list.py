"""Run the FMKorea list workflow and retrieve its JSON artifact."""

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
WORKFLOW_FILE = "fmkorea-list.yml"
REF = "master"
API_BASE = "https://api.github.com"
ARTIFACT_NAME = "fmkorea-list"
POLL_INTERVAL_SECONDS = 5
DISPATCH_TIMEOUT_SECONDS = 45
RUN_TIMEOUT_SECONDS = 900


def validate_pages(value: str) -> str:
    """Validate and normalize the workflow's page-count input."""
    if not value.isdigit() or not 1 <= int(value) <= 6:
        raise GitHubError("pages must be an integer from 1 through 6")
    return str(int(value))


def workflow_url() -> str:
    return f"{API_BASE}/repos/{OWNER}/{REPOSITORY}/actions/workflows/{WORKFLOW_FILE}"


def dispatch_workflow(session: requests.Session, pages: str) -> None:
    response = session.post(
        f"{workflow_url()}/dispatches",
        json={"ref": REF, "inputs": {"pages": pages}},
        timeout=30,
    )
    if response.status_code != 204:
        detail = response.text[:500].replace("\n", " ")
        raise GitHubError(
            f"Workflow dispatch failed with status {response.status_code}: {detail}"
        )


def list_runs_for_workflow(session: requests.Session) -> list[dict[str, Any]]:
    payload = request_json(
        session,
        "GET",
        f"{workflow_url()}/runs",
        params={
            "branch": REF,
            "event": "workflow_dispatch",
            "per_page": 20,
        },
    )
    runs = payload.get("workflow_runs", [])
    if not isinstance(runs, list):
        raise GitHubError("GitHub returned an invalid workflow_runs response")
    return [run for run in runs if isinstance(run, dict)]


def wait_for_new_run(
    session: requests.Session,
    known_run_ids: set[int],
) -> dict[str, Any]:
    deadline = time.monotonic() + DISPATCH_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        for run in list_runs_for_workflow(session):
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
    if not isinstance(artifacts, list):
        raise GitHubError("GitHub returned an invalid artifacts response")
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
    response = session.get(download_url, timeout=60)
    if not response.ok:
        detail = response.text[:500].replace("\n", " ")
        raise GitHubError(
            f"Artifact download failed with {response.status_code}: {detail}"
        )

    try:
        with zipfile.ZipFile(BytesIO(response.content)) as archive:
            result_name = next(
                (name for name in archive.namelist() if Path(name).name == "result.json"),
                None,
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
    parser = argparse.ArgumentParser(
        description="Request an FMKorea list crawl and download its JSON artifact"
    )
    parser.add_argument(
        "--pages",
        default="6",
        help="Number of list pages to crawl (1-6, default: 6)",
    )
    parser.add_argument("-o", "--output", type=Path, help="Optional path to save result.json")
    args = parser.parse_args()

    try:
        pages = validate_pages(args.pages)
        session = create_session(load_token())
        known_run_ids = {
            run_id
            for run in list_runs_for_workflow(session)
            if isinstance((run_id := run.get("id")), int)
        }
        dispatch_workflow(session, pages)
        run = wait_for_new_run(session, known_run_ids)
        completed_run = wait_for_completion(session, run)
        run_id = completed_run.get("id")
        if not isinstance(run_id, int):
            raise GitHubError("Completed workflow run did not contain a valid run ID")
        result = download_result(session, run_id)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print(f"saved result to {args.output}")
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (GitHubError, requests.RequestException) as error:
        print(f"FMKorea list request failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

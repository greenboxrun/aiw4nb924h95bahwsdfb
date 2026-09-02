"""Central enablement settings for scheduled GitHub Actions jobs."""

from __future__ import annotations

import argparse


SCHEDULED_JOB_ENABLED = {
    "topic.fmkorea.posts": False,
}


def is_enabled(job_name: str) -> bool:
    try:
        return SCHEDULED_JOB_ENABLED[job_name]
    except KeyError as error:
        raise ValueError(f"Unknown scheduled job: {job_name}") from error


def main() -> int:
    parser = argparse.ArgumentParser(description="Read a scheduled-job enablement flag")
    parser.add_argument("--job", required=True, choices=sorted(SCHEDULED_JOB_ENABLED))
    args = parser.parse_args()
    print("true" if is_enabled(args.job) else "false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

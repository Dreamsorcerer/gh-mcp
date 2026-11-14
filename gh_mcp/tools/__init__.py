"""Tool assembly."""

from typing import Final

from aiohttp_mcp_server import Tool

from . import actions_ci, advisories, feedback, git, issues, pulls, raw, repos
from ._base import GATE, Gate

__all__ = ("ALL_TOOLS", "GATE", "Gate")

ALL_TOOLS: Final = (
    Tool(feedback.report_missing_access, read_only=True),
    Tool(git.create_branch, destructive=False, idempotent=True),
    Tool(git.commit_files, destructive=False),
    # Issues and pull requests: the day-to-day working set.
    Tool(issues.get_issue, read_only=True, idempotent=True),
    Tool(issues.list_issues, read_only=True, idempotent=True),
    Tool(issues.search_issues, read_only=True, idempotent=True),
    Tool(pulls.get_pr, read_only=True, idempotent=True),
    Tool(pulls.get_pr_diff, read_only=True, idempotent=True),
    Tool(pulls.get_pr_files, read_only=True, idempotent=True),
    Tool(pulls.list_prs, read_only=True, idempotent=True),
    # Repository contents.
    Tool(repos.get_file, read_only=True, idempotent=True),
    Tool(repos.list_dir, read_only=True, idempotent=True),
    Tool(repos.search_code, read_only=True, idempotent=True),
    # CI results.
    Tool(actions_ci.list_workflow_runs, read_only=True, idempotent=True),
    Tool(actions_ci.list_run_jobs, read_only=True, idempotent=True),
    Tool(actions_ci.get_job_logs, read_only=True, idempotent=True),
    Tool(actions_ci.list_releases, read_only=True, idempotent=True),
    # Writes.
    Tool(issues.add_comment, destructive=False),
    Tool(issues.create_issue, destructive=False),
    Tool(pulls.create_pr, destructive=False),
    Tool(pulls.update_pr, destructive=False, idempotent=True),
    Tool(repos.create_fork, destructive=False, idempotent=True),
    # Security advisories.
    Tool(advisories.list_repo_advisories, read_only=True, idempotent=True),
    Tool(advisories.get_advisory, read_only=True, idempotent=True),
    Tool(advisories.create_advisory_fork, destructive=False, idempotent=True),
    Tool(advisories.update_advisory, destructive=False, idempotent=True),
    # Passthrough.
    Tool(raw.gh_api_get, read_only=True, idempotent=True),
)

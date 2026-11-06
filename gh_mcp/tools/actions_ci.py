"""GitHub Actions tools (read-only): workflow runs and job logs."""

import re
from typing import Annotated, Literal, TypedDict

from aiohttp_mcp import Doc, MCPRequest, ToolError
from pydantic import Field

from ..github import paginate
from ._base import GATE, Repo

RunStatus = Literal[
    "completed", "action_required", "cancelled", "failure", "neutral",
    "skipped", "stale", "success", "timed_out", "in_progress", "queued",
    "requested", "waiting", "pending",
]


class RunSummary(TypedDict):
    id: int
    name: str
    head_branch: str
    event: str
    status: str
    conclusion: str | None
    created_at: str
    url: str


class Release(TypedDict):
    tag: str
    name: str | None
    published_at: str | None
    url: str


async def list_workflow_runs(
    ctx: MCPRequest,
    repo: Repo,
    branch: str | None = None,
    status: Annotated[
        RunStatus | None,
        Doc("Only runs with this status or conclusion, e.g. 'failure'"),
    ] = None,
    created: Annotated[
        str | None,
        Doc("Created-date filter, e.g. '>=2026-11-01' or '2026-11-01..2026-11-03'"),
    ] = None,
    limit: int = 20,
    page: Annotated[
        int, Doc("Page of `limit` runs, for paging further back"), Field(ge=1)
    ] = 1,
) -> list[RunSummary]:
    """List recent workflow (CI) runs, newest first."""
    gate = ctx.http.app[GATE]
    gate.check(ctx, "list_workflow_runs", "actions:read", repo)
    params: dict[str, str] = {}
    if branch:
        params["branch"] = branch
    if status:
        params["status"] = status
    if created:
        params["created"] = created
    items = await paginate(
        gate.gh,
        f"/repos/{repo}/actions/runs",
        params=params,
        limit=limit,
        items_key="workflow_runs",
        page=page,
    )
    return [
        {
            "id": run["id"],
            "name": run["name"],
            "head_branch": run["head_branch"],
            "event": run["event"],
            "status": run["status"],
            "conclusion": run["conclusion"],
            "created_at": run["created_at"],
            "url": run["html_url"],
        }
        for run in items
    ]


async def list_run_jobs(
    ctx: MCPRequest, repo: Repo, run_id: int
) -> list[RunSummary]:
    """List the jobs of a workflow run, with their ids for get_job_logs."""
    gate = ctx.http.app[GATE]
    gate.check(ctx, "list_run_jobs", "actions:read", repo)
    items = await paginate(
        gate.gh,
        f"/repos/{repo}/actions/runs/{run_id}/jobs",
        limit=100,
        items_key="jobs",
    )
    return [
        {
            "id": job["id"],
            "name": job["name"],
            "head_branch": job["head_branch"],
            "event": "",
            "status": job["status"],
            "conclusion": job["conclusion"],
            "created_at": job["created_at"],
            "url": job["html_url"],
        }
        for job in items
    ]


async def get_job_logs(
    ctx: MCPRequest,
    repo: Repo,
    job_id: int,
    tail: Annotated[
        int, Doc("Last N lines to return; 0 returns the whole log"), Field(ge=0)
    ] = 500,
    offset: Annotated[
        int | None, Doc("1-based line number to read from, instead of tailing"), Field(ge=1)
    ] = None,
    limit: Annotated[
        int, Doc("Max lines returned in offset/grep mode"), Field(ge=1)
    ] = 500,
    grep: Annotated[
        str | None, Doc("Regex: return only matching lines, as 'lineno:text'")
    ] = None,
) -> str:
    """Get the log output of a workflow job."""
    gate = ctx.http.app[GATE]
    gate.check(ctx, "get_job_logs", "actions:read", repo)
    async with gate.gh.get(f"/repos/{repo}/actions/jobs/{job_id}/logs") as resp:
        text = await resp.text()
    lines = text.splitlines()
    if grep is not None:
        return _grep_log(lines, grep, limit)
    if offset is not None:
        return _slice_log(lines, offset, limit)
    if tail and len(lines) > tail:
        lines = [f"... ({len(lines) - tail} earlier lines omitted)"] + lines[-tail:]
    return "\n".join(lines)


def _grep_log(lines: list[str], pattern: str, limit: int) -> str:
    """Matching lines as ``lineno:text``, capped at ``limit`` matches."""
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise ToolError(f"Invalid grep regex: {exc}") from None
    matches = [f"{n}:{line}" for n, line in enumerate(lines, 1) if regex.search(line)]
    if not matches:
        return f"No matches in {len(lines)} log lines."
    out = matches[:limit]
    if len(matches) > limit:
        out.append(f"... ({len(matches) - limit} more matching lines omitted)")
    return "\n".join(out)


def _slice_log(lines: list[str], offset: int, limit: int) -> str:
    """``limit`` lines from 1-based ``offset``, with omission markers."""
    total = len(lines)
    if offset > total:
        raise ToolError(f"offset {offset} is past the end: the log has {total} lines")
    end = min(offset - 1 + limit, total)
    out = lines[offset - 1:end]
    if offset > 1:
        out.insert(0, f"... ({offset - 1} earlier lines omitted)")
    if end < total:
        out.append(f"... ({total - end} later lines omitted)")
    return "\n".join(out)


async def list_releases(
    ctx: MCPRequest, repo: Repo, limit: int = 10
) -> list[Release]:
    """List a repository's releases, newest first."""
    gate = ctx.http.app[GATE]
    gate.check(ctx, "list_releases", "release:read", repo)
    items = await paginate(gate.gh, f"/repos/{repo}/releases", limit=limit)
    return [
        {
            "tag": release["tag_name"],
            "name": release["name"],
            "published_at": release["published_at"],
            "url": release["html_url"],
        }
        for release in items
    ]

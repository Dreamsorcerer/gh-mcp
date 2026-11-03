"""GitHub Actions tools (read-only): workflow runs and job logs."""

from typing import TypedDict

from aiohttp_mcp import MCPRequest

from ..github import paginate
from ._base import GATE, Repo


class RunSummary(TypedDict):
    id: int
    name: str
    head_branch: str
    event: str
    status: str
    conclusion: str | None
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
    limit: int = 20,
) -> list[RunSummary]:
    """List recent workflow (CI) runs, newest first."""
    gate = ctx.http.app[GATE]
    gate.check(ctx, "list_workflow_runs", "actions:read", repo)
    params = {"branch": branch} if branch else None
    items = await paginate(gate.gh, 
        f"/repos/{repo}/actions/runs",
        params=params,
        limit=limit,
        items_key="workflow_runs",
    )
    return [
        {
            "id": run["id"],
            "name": run["name"],
            "head_branch": run["head_branch"],
            "event": run["event"],
            "status": run["status"],
            "conclusion": run["conclusion"],
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
    items = await paginate(gate.gh, 
        f"/repos/{repo}/actions/runs/{run_id}/jobs", limit=100, items_key="jobs"
    )
    return [
        {
            "id": job["id"],
            "name": job["name"],
            "head_branch": job["head_branch"],
            "event": "",
            "status": job["status"],
            "conclusion": job["conclusion"],
            "url": job["html_url"],
        }
        for job in items
    ]


async def get_job_logs(
    ctx: MCPRequest, repo: Repo, job_id: int, tail: int = 500
) -> str:
    """Get the log output of a workflow job (last ``tail`` lines)."""
    gate = ctx.http.app[GATE]
    gate.check(ctx, "get_job_logs", "actions:read", repo)
    async with gate.gh.get(f"/repos/{repo}/actions/jobs/{job_id}/logs") as resp:
        text = await resp.text()
    lines = text.splitlines()
    if len(lines) > tail:
        lines = [f"... ({len(lines) - tail} earlier lines omitted)"] + lines[-tail:]
    return "\n".join(lines)


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

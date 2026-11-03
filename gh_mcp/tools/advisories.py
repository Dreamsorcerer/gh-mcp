"""Repository security advisory (GHSA) tools."""

from typing import TypedDict

from aiohttp_mcp import MCPRequest

from ..github import paginate
from ._base import GATE, Repo


class AdvisorySummary(TypedDict):
    ghsa_id: str
    summary: str
    severity: str | None
    state: str
    url: str


class AdvisoryDetail(TypedDict):
    ghsa_id: str
    summary: str
    description: str | None
    severity: str | None
    state: str
    cve_id: str | None
    url: str


class RepoRef(TypedDict):
    full_name: str
    url: str


async def list_repo_advisories(ctx: MCPRequest, repo: Repo) -> list[AdvisorySummary]:
    """List a repository's security advisories (GHSAs)."""
    gate = ctx.http.app[GATE]
    gate.check(ctx, "list_repo_advisories", "ghsa:read", repo)
    items = await paginate(gate.gh, f"/repos/{repo}/security-advisories", limit=100)
    return [
        {
            "ghsa_id": adv["ghsa_id"],
            "summary": adv["summary"],
            "severity": adv["severity"],
            "state": adv["state"],
            "url": adv["html_url"],
        }
        for adv in items
    ]


async def get_advisory(ctx: MCPRequest, repo: Repo, ghsa_id: str) -> AdvisoryDetail:
    """Get a repository security advisory's full details."""
    gate = ctx.http.app[GATE]
    gate.check(ctx, "get_advisory", "ghsa:read", repo)
    async with gate.gh.get(f"/repos/{repo}/security-advisories/{ghsa_id}") as resp:
        data = await resp.json()
    return {
        "ghsa_id": data["ghsa_id"],
        "summary": data["summary"],
        "description": data["description"],
        "severity": data["severity"],
        "state": data["state"],
        "cve_id": data["cve_id"],
        "url": data["html_url"],
    }


async def create_advisory_fork(ctx: MCPRequest, repo: Repo, ghsa_id: str) -> RepoRef:
    """Create the temporary private fork for collaborating on a GHSA fix."""
    gate = ctx.http.app[GATE]
    async with gate.write(ctx, "create_advisory_fork", "ghsa:create_fork", repo) as write:
        async with gate.gh.post(f"/repos/{repo}/security-advisories/{ghsa_id}/forks") as resp:
            data = await resp.json()
        write.url = data["html_url"]
    return {"full_name": data["full_name"], "url": write.url}

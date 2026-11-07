"""Repository security advisory (GHSA) tools."""

from typing import Annotated, Literal, NotRequired, TypedDict

from aiohttp_mcp import Doc, MCPRequest, ToolError

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


class VulnerablePackage(TypedDict):
    ecosystem: Literal[
        "rubygems", "npm", "pip", "maven", "nuget", "composer", "go",
        "rust", "erlang", "actions", "pub", "other", "swift",
    ]
    name: NotRequired[str | None]


class Vulnerability(TypedDict):
    package: VulnerablePackage
    vulnerable_version_range: NotRequired[str | None]
    patched_versions: NotRequired[str | None]
    vulnerable_functions: NotRequired[list[str] | None]


class Credit(TypedDict):
    login: str
    type: Literal[
        "analyst", "finder", "reporter", "coordinator", "remediation_developer",
        "remediation_reviewer", "remediation_verifier", "tool", "sponsor", "other",
    ]


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


async def update_advisory(
    ctx: MCPRequest,
    repo: Repo,
    ghsa_id: str,
    summary: str | None = None,
    description: str | None = None,
    vulnerabilities: Annotated[
        list[Vulnerability] | None,
        Doc("Affected packages/versions; replaces the existing list"),
    ] = None,
    cwe_ids: Annotated[list[str] | None, Doc("CWE ids, e.g. ['CWE-400']")] = None,
    credits: Annotated[
        list[Credit] | None, Doc("Credited users; replaces the existing list")
    ] = None,
    severity: Literal["critical", "high", "medium", "low"] | None = None,
    cvss_vector_string: Annotated[
        str | None, Doc("CVSS vector, e.g. 'CVSS:3.1/AV:N/AC:L/...'")
    ] = None,
    state: Literal["closed", "draft"] | None = None,
) -> AdvisoryDetail:
    """Update an unpublished repository security advisory (GHSA).

    Omitted fields are left unchanged; list fields replace the existing
    list wholesale.
    """
    gate = ctx.http.app[GATE]
    async with gate.write(ctx, "update_advisory", "ghsa:update", repo) as write:
        fields = {
            "summary": summary,
            "description": description,
            "vulnerabilities": vulnerabilities,
            "cwe_ids": cwe_ids,
            "credits": credits,
            "severity": severity,
            "cvss_vector_string": cvss_vector_string,
            "state": state,
        }
        payload = {k: v for k, v in fields.items() if v is not None}
        if not payload:
            raise ToolError("Nothing to update: provide at least one field")
        if severity is not None and cvss_vector_string is not None:
            raise ToolError("Set either severity or cvss_vector_string, not both")
        async with gate.gh.get(f"/repos/{repo}/security-advisories/{ghsa_id}") as resp:
            current = await resp.json()
        if current["state"] == "published":
            raise ToolError(
                f"{ghsa_id} is published; only unpublished advisories can be edited"
            )
        async with gate.gh.patch(
            f"/repos/{repo}/security-advisories/{ghsa_id}", json=payload
        ) as resp:
            data = await resp.json()
        write.url = data["html_url"]
    return {
        "ghsa_id": data["ghsa_id"],
        "summary": data["summary"],
        "description": data["description"],
        "severity": data["severity"],
        "state": data["state"],
        "cve_id": data["cve_id"],
        "url": data["html_url"],
    }

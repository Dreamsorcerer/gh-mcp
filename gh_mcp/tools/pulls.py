"""Pull-request tools."""

from typing import Any, Final, Literal, TypedDict

from aiohttp import hdrs
from aiohttp_mcp import MCPRequest, Tool, ToolError

from ..github import paginate
from ..policy import apply_prefix, apply_suffix
from ._base import GATE, Repo

_DIFF = "application/vnd.github.diff"


class PRSummary(TypedDict):
    number: int
    title: str
    state: str
    author: str
    draft: bool
    base: str
    head: str
    url: str


class ReviewInfo(TypedDict):
    author: str
    state: str
    body: str | None


class PRDetail(TypedDict):
    number: int
    title: str
    state: str
    author: str
    draft: bool
    base: str
    head: str
    body: str | None
    mergeable_state: str
    reviews: list[ReviewInfo]
    url: str


class FileChange(TypedDict):
    path: str
    status: str
    additions: int
    deletions: int


class PRRef(TypedDict):
    number: int
    url: str
    draft: bool


def pr_summary(item: dict[str, Any]) -> PRSummary:
    return {
        "number": item["number"],
        "title": item["title"],
        "state": item["state"],
        "author": item["user"]["login"],
        "draft": item["draft"],
        "base": item["base"]["ref"],
        "head": item["head"]["label"],
        "url": item["html_url"],
    }


async def list_prs(
    ctx: MCPRequest,
    repo: Repo,
    state: Literal["open", "closed", "all"] = "open",
    base: str | None = None,
    limit: int = 30,
) -> list[PRSummary]:
    """List a repository's pull requests, newest first."""
    gate = ctx.http.app[GATE]
    gate.check(ctx, "list_prs", "pr:read", repo)
    params: dict[str, str] = {"state": state}
    if base:
        params["base"] = base
    items = await paginate(gate.gh, f"/repos/{repo}/pulls", params=params, limit=limit)
    return [pr_summary(item) for item in items]


async def get_pr(ctx: MCPRequest, repo: Repo, number: int) -> PRDetail:
    """Get a pull request with its full body and reviews."""
    gate = ctx.http.app[GATE]
    gate.check(ctx, "get_pr", "pr:read", repo)
    async with gate.gh.get(f"/repos/{repo}/pulls/{number}") as resp:
        data = await resp.json()
    raw_reviews = await paginate(gate.gh, f"/repos/{repo}/pulls/{number}/reviews", limit=100)
    reviews: list[ReviewInfo] = [
        {"author": r["user"]["login"], "state": r["state"], "body": r["body"]}
        for r in raw_reviews
    ]
    summary = pr_summary(data)
    return {
        "number": summary["number"],
        "title": summary["title"],
        "state": summary["state"],
        "author": summary["author"],
        "draft": summary["draft"],
        "base": summary["base"],
        "head": summary["head"],
        "body": data["body"],
        "mergeable_state": data["mergeable_state"],
        "reviews": reviews,
        "url": summary["url"],
    }


async def get_pr_diff(ctx: MCPRequest, repo: Repo, number: int) -> str:
    """Get a pull request's changes as a unified diff."""
    gate = ctx.http.app[GATE]
    gate.check(ctx, "get_pr_diff", "pr:read", repo)
    async with gate.gh.get(
        f"/repos/{repo}/pulls/{number}", headers={hdrs.ACCEPT: _DIFF}
    ) as resp:
        return await resp.text()


async def get_pr_files(ctx: MCPRequest, repo: Repo, number: int) -> list[FileChange]:
    """List the files changed by a pull request."""
    gate = ctx.http.app[GATE]
    gate.check(ctx, "get_pr_files", "pr:read", repo)
    items = await paginate(gate.gh, f"/repos/{repo}/pulls/{number}/files", limit=300)
    return [
        {
            "path": f["filename"],
            "status": f["status"],
            "additions": f["additions"],
            "deletions": f["deletions"],
        }
        for f in items
    ]


async def create_pr(
    ctx: MCPRequest, repo: Repo, title: str, body: str, head: str, base: str
) -> PRRef:
    """Open a pull request."""
    gate = ctx.http.app[GATE]
    async with gate.write(ctx, "create_pr", "pr:create", repo) as write:
        constraints = write.constraints
        payload: dict[str, object] = {
            "title": apply_prefix(title, constraints.get("title_prefix", "")),
            "body": apply_suffix(apply_prefix(body, constraints.get("body_prefix", "")),
                                 constraints.get("body_suffix", "")),
            "head": head,
            "base": base,
        }
        if constraints.get("draft_only", False):
            payload["draft"] = True
        async with gate.gh.post(f"/repos/{repo}/pulls", json=payload) as resp:
            data = await resp.json()
        write.url = url = data["html_url"]
    return {"number": data["number"], "url": url, "draft": data["draft"]}


async def update_pr(
    ctx: MCPRequest,
    repo: Repo,
    number: int,
    title: str | None = None,
    body: str | None = None,
    base: str | None = None,
) -> PRRef:
    """Update a pull request's title, body, or base branch."""
    gate = ctx.http.app[GATE]
    async with gate.write(ctx, "update_pr", "pr:update", repo) as write:
        constraints = write.constraints
        payload: dict[str, object] = {}
        if title is not None:
            payload["title"] = apply_prefix(title, constraints.get("title_prefix", ""))
        if body is not None:
            payload["body"] = apply_suffix(
                apply_prefix(body, constraints.get("body_prefix", "")),
                constraints.get("body_suffix", ""),
            )
        if base is not None:
            payload["base"] = base
        if not payload:
            raise ToolError("Nothing to update: provide title, body, or base")
        async with gate.gh.patch(f"/repos/{repo}/pulls/{number}", json=payload) as resp:
            data = await resp.json()
        write.url = data["html_url"]
    return {"number": data["number"], "url": write.url, "draft": data["draft"]}


TOOLS: Final = (
    Tool(list_prs, read_only=True, idempotent=True),
    Tool(get_pr, read_only=True, idempotent=True),
    Tool(get_pr_diff, read_only=True, idempotent=True),
    Tool(get_pr_files, read_only=True, idempotent=True),
    Tool(create_pr, destructive=False),
    Tool(update_pr, destructive=False, idempotent=True),
)

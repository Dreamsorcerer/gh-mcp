"""Issue tools."""

from typing import Any, Final, Literal, TypedDict

from aiohttp_mcp import MCPRequest, Tool

from ..github import paginate
from ..policy import apply_prefix, apply_suffix
from ._base import GATE, Repo


class IssueSummary(TypedDict):
    number: int
    title: str
    state: str
    author: str
    labels: list[str]
    is_pull_request: bool
    url: str


class CommentInfo(TypedDict):
    author: str
    created_at: str
    body: str


class IssueDetail(TypedDict):
    number: int
    title: str
    state: str
    author: str
    labels: list[str]
    body: str | None
    comments: list[CommentInfo]
    url: str


class SearchIssueSummary(TypedDict):
    repository: str
    number: int
    title: str
    state: str
    is_pull_request: bool
    url: str


class IssueRef(TypedDict):
    number: int
    url: str


class CommentRef(TypedDict):
    url: str


def issue_summary(item: dict[str, Any]) -> IssueSummary:
    return {
        "number": item["number"],
        "title": item["title"],
        "state": item["state"],
        "author": item["user"]["login"],
        "labels": [label["name"] for label in item["labels"]],
        "is_pull_request": "pull_request" in item,
        "url": item["html_url"],
    }


async def list_issues(
    ctx: MCPRequest,
    repo: Repo,
    state: Literal["open", "closed", "all"] = "open",
    labels: list[str] | None = None,
    limit: int = 30,
) -> list[IssueSummary]:
    """List a repository's issues and pull requests, newest first."""
    gate = ctx.http.app[GATE]
    gate.check(ctx, "list_issues", "issue:read", repo)
    params: dict[str, str] = {"state": state}
    if labels:
        params["labels"] = ",".join(labels)
    items = await paginate(gate.gh, f"/repos/{repo}/issues", params=params, limit=limit)
    return [issue_summary(item) for item in items]


async def get_issue(ctx: MCPRequest, repo: Repo, number: int) -> IssueDetail:
    """Get an issue (or PR) with its full body and all comments."""
    gate = ctx.http.app[GATE]
    gate.check(ctx, "get_issue", "issue:read", repo)
    async with gate.gh.get(f"/repos/{repo}/issues/{number}") as resp:
        data = await resp.json()
    raw_comments = await paginate(gate.gh, 
        f"/repos/{repo}/issues/{number}/comments", limit=200
    )
    comments: list[CommentInfo] = [
        {"author": c["user"]["login"], "created_at": c["created_at"], "body": c["body"]}
        for c in raw_comments
    ]
    summary = issue_summary(data)
    return {
        "number": summary["number"],
        "title": summary["title"],
        "state": summary["state"],
        "author": summary["author"],
        "labels": summary["labels"],
        "body": data["body"],
        "comments": comments,
        "url": summary["url"],
    }


async def search_issues(
    ctx: MCPRequest, query: str, limit: int = 20
) -> list[SearchIssueSummary]:
    """Search issues and PRs across GitHub (GitHub search syntax)."""
    gate = ctx.http.app[GATE]
    gate.check(ctx, "search_issues", "search:read", "")
    items = await paginate(gate.gh, 
        "/search/issues", params={"q": query}, limit=limit, items_key="items"
    )
    return [
        {
            "repository": "/".join(item["repository_url"].rsplit("/", 2)[-2:]),
            "number": item["number"],
            "title": item["title"],
            "state": item["state"],
            "is_pull_request": "pull_request" in item,
            "url": item["html_url"],
        }
        for item in items
    ]


async def create_issue(
    ctx: MCPRequest,
    repo: Repo,
    title: str,
    body: str,
    labels: list[str] | None = None,
) -> IssueRef:
    """Open a new issue (issues MUST be concise and to the point)."""
    gate = ctx.http.app[GATE]
    async with gate.write(ctx, "create_issue", "issue:create", repo) as write:
        constraints = write.constraints
        payload: dict[str, object] = {
            "title": apply_prefix(title, constraints.get("title_prefix", "")),
            "body": apply_suffix(apply_prefix(body, constraints.get("body_prefix", "")),
                                 constraints.get("body_suffix", "")),
        }
        if labels:
            payload["labels"] = labels
        async with gate.gh.post(f"/repos/{repo}/issues", json=payload) as resp:
            data = await resp.json()
        write.url = url = data["html_url"]
    return {"number": data["number"], "url": url}


async def add_comment(ctx: MCPRequest, repo: Repo, number: int, body: str) -> CommentRef:
    """Comment on an issue or pull request."""
    gate = ctx.http.app[GATE]
    async with gate.write(ctx, "add_comment", "issue:comment", repo) as write:
        constraints = write.constraints
        payload = {
            "body": apply_suffix(apply_prefix(body, constraints.get("body_prefix", "")),
                                 constraints.get("body_suffix", ""))
        }
        async with gate.gh.post(
            f"/repos/{repo}/issues/{number}/comments", json=payload
        ) as resp:
            data = await resp.json()
        write.url = data["html_url"]
    return {"url": write.url}


TOOLS: Final = (
    Tool(list_issues, read_only=True, idempotent=True),
    Tool(get_issue, read_only=True, idempotent=True),
    Tool(search_issues, read_only=True, idempotent=True),
    Tool(create_issue, destructive=False),
    Tool(add_comment, destructive=False),
)

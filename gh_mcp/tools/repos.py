"""Repository content, search, and fork tools."""

import base64
from typing import TypedDict

from aiohttp_mcp_server import MCPRequest, ToolError

from ..github import paginate
from ._base import GATE, Repo


class FileContent(TypedDict):
    path: str
    content: str
    sha: str
    size: int


class DirEntry(TypedDict):
    name: str
    path: str
    type: str
    size: int


class CodeMatch(TypedDict):
    repository: str
    path: str
    url: str


class ForkRef(TypedDict):
    full_name: str
    default_branch: str
    url: str


async def get_file(
    ctx: MCPRequest, repo: Repo, path: str, ref: str | None = None
) -> FileContent:
    """Get a file's text content at a ref (default branch when omitted)."""
    gate = ctx.http.app[GATE]
    gate.check(ctx, "get_file", "code:read", repo)
    params = {"ref": ref} if ref else None
    async with gate.gh.get(f"/repos/{repo}/contents/{path}", params=params) as resp:
        data = await resp.json()
    if data.get("type") != "file":
        raise ToolError(f"{path} is not a file; use list_dir for directories")
    try:
        content = base64.b64decode(data["content"]).decode()
    except (ValueError, UnicodeDecodeError):
        raise ToolError(f"{path} is not valid UTF-8 text") from None
    return {
        "path": data["path"],
        "content": content,
        "sha": data["sha"],
        "size": data["size"],
    }


async def list_dir(
    ctx: MCPRequest, repo: Repo, path: str = "", ref: str | None = None
) -> list[DirEntry]:
    """List a directory's entries at a ref (repository root by default)."""
    gate = ctx.http.app[GATE]
    gate.check(ctx, "list_dir", "code:read", repo)
    params = {"ref": ref} if ref else None
    async with gate.gh.get(f"/repos/{repo}/contents/{path}", params=params) as resp:
        data = await resp.json()
    if isinstance(data, dict):
        raise ToolError(f"{path} is a file; use get_file to read it")
    return [
        {"name": e["name"], "path": e["path"], "type": e["type"], "size": e["size"]}
        for e in data
    ]


async def search_code(ctx: MCPRequest, query: str, limit: int = 20) -> list[CodeMatch]:
    """Search code across GitHub (GitHub code-search syntax, e.g. 'repo:o/r term')."""
    gate = ctx.http.app[GATE]
    gate.check(ctx, "search_code", "search:read", "")
    items = await paginate(gate.gh, 
        "/search/code", params={"q": query}, limit=limit, items_key="items"
    )
    return [
        {
            "repository": item["repository"]["full_name"],
            "path": item["path"],
            "url": item["html_url"],
        }
        for item in items
    ]


async def create_fork(ctx: MCPRequest, repo: Repo) -> ForkRef:
    """Fork a repository to the token's account.

    Returns the existing fork if one is already present. Forking is
    asynchronous on GitHub's side: the fork can take a few seconds to
    become usable, so an immediate create_branch on it may briefly 404.
    """
    gate = ctx.http.app[GATE]
    async with gate.write(
        ctx, "create_fork", "repo:fork", repo, detail="fork the repository"
    ) as write:
        async with gate.gh.post(f"/repos/{repo}/forks") as resp:
            data = await resp.json()
        write.url = data["html_url"]
    return {
        "full_name": data["full_name"],
        "default_branch": data["default_branch"],
        "url": data["html_url"],
    }

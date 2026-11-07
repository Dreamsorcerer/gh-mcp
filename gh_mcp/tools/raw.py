"""Raw GET passthrough for read endpoints without a dedicated tool."""

import re

from aiohttp_mcp import MCPRequest, ToolError

from ._base import GATE

_REPO_PATH = re.compile(r"/repos/([^/]+)/([^/]+)(?:/|$)")


async def gh_api_get(
    ctx: MCPRequest, path: str, params: dict[str, str] | None = None
) -> dict[str, object]:
    """Raw GitHub REST GET for repository endpoints without a dedicated tool.

    ``path`` must be under a repository, e.g. ``/repos/{owner}/{repo}/branches``;
    account- and org-level endpoints are not reachable through this tool.
    """
    gate = ctx.http.app[GATE]
    if not path.startswith("/") or ".." in path:
        raise ToolError("path must be an absolute GitHub API path, e.g. /repos/o/r/tags")
    matched = _REPO_PATH.match(path)
    if matched is None:
        raise ToolError("only repository paths (/repos/{owner}/{repo}/...) are allowed")
    gate.check(ctx, "gh_api_get", "raw:read", f"{matched[1]}/{matched[2]}", path=path)
    async with gate.gh.get(path, params=params) as resp:
        if resp.content_type == "application/json":
            data = await resp.json()
        else:
            data = await resp.text()
    return data if isinstance(data, dict) else {"result": data}

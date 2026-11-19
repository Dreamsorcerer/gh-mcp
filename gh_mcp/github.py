import json
from collections.abc import Mapping
from typing import Any, Final

from aiohttp import ClientHandlerType, ClientRequest, ClientResponse, ClientSession, hdrs
from aiohttp_mcp_server import ToolError

MAX_PAGES: Final = 10


class GitHubError(ToolError):
    """An error response from GitHub, worded for the model to act on."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


async def _check_errors(req: ClientRequest, handler: ClientHandlerType) -> ClientResponse:
    """Map error responses to a GitHubError."""
    resp = await handler(req)
    if resp.ok:
        return resp
    message = body = await resp.text()
    try:
        parsed = json.loads(body)
    except ValueError:
        pass
    else:
        if isinstance(parsed, dict) and isinstance(parsed.get("message"), str):
            message = parsed["message"]
    retry_after = resp.headers.get(hdrs.RETRY_AFTER)
    if resp.status in {403, 429} and retry_after is not None:
        raise GitHubError(
            resp.status,
            f"GitHub rate limited this call. Retry in {retry_after} seconds: {message}",
        )
    raise GitHubError(resp.status, f"GitHub API error (HTTP {resp.status}): {message}")


def github_session(token: str, api_url: str) -> ClientSession:
    """A session preconfigured for the GitHub REST API."""
    return ClientSession(
        base_url=api_url,
        headers={
            hdrs.ACCEPT: "application/vnd.github+json",
            hdrs.AUTHORIZATION: f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        middlewares=(_check_errors,),
    )


async def paginate(
    session: ClientSession,
    path: str,
    *,
    params: Mapping[str, str] | None = None,
    limit: int,
    items_key: str | None = None,
    page: int = 1,
) -> list[Any]:
    """Follow pages until ``limit`` items (capped at :data:`MAX_PAGES` pages).

    ``items_key`` unwraps envelope responses (search, workflow runs);
    ``page`` starts at that page (pages are ``limit``-sized up to 100).
    """
    items: list[Any] = []
    per_page = min(max(limit, 1), 100)
    for page_num in range(page, page + MAX_PAGES):
        page_params = dict(params or {}) | {"per_page": str(per_page), "page": str(page_num)}
        async with session.get(path, params=page_params) as resp:
            data = await resp.json()
        if items_key is not None:
            assert isinstance(data, dict)
            data = data[items_key]
        assert isinstance(data, list)
        items.extend(data)
        if len(items) >= limit or len(data) < per_page:
            break
    return items[:limit]

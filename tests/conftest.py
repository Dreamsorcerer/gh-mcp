import json
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, cast

import aiohttp_mcp_server
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient
from pytest_aiohttp import AiohttpClient

from gh_mcp.audit import AuditLog
from gh_mcp.config import Rule
from gh_mcp.github import github_session
from gh_mcp.policy import PolicyEngine
from gh_mcp.tools import ALL_TOOLS, GATE, Gate

Client = TestClient[web.Request, web.Application]

PROTOCOL_VERSION = "2026-07-28"


class FakeGitHub:
    """A programmable fake of the GitHub REST API."""

    def __init__(self) -> None:
        # (method, path) -> (status, body, headers)
        self.routes: dict[tuple[str, str], tuple[int, object, dict[str, str]]] = {}
        # Recorded (method, path, query, json_body, headers) per request.
        self.requests: list[tuple[str, str, dict[str, str], object, dict[str, str]]] = []
        self.base_url = ""

    def add(
        self,
        method: str,
        path: str,
        body: object = None,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.routes[(method, path)] = (status, body, headers or {})

    async def handler(self, request: web.Request) -> web.Response:
        body = None
        if request.can_read_body and request.content_type == "application/json":
            body = await request.json()
        self.requests.append(
            (request.method, request.path, dict(request.query), body, dict(request.headers))
        )
        route = self.routes.get((request.method, request.path))
        if route is None:
            return web.json_response({"message": "Not Found"}, status=404)
        status, payload, headers = route
        if isinstance(payload, str):
            return web.Response(text=payload, status=status, headers=headers)
        return web.Response(
            text=json.dumps(payload),
            status=status,
            content_type="application/json",
            headers=headers,
        )


@pytest.fixture
def fake_github() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
def audit_path(tmp_path: Path) -> Path:
    return tmp_path / "audit.jsonl"


MakeApp = Callable[..., Awaitable[Client]]


@pytest.fixture
def make_app(
    aiohttp_client: AiohttpClient, fake_github: FakeGitHub, audit_path: Path
) -> MakeApp:
    """An MCP endpoint wired to the fake GitHub, with the given policy rules."""

    async def maker(rules: list[Rule]) -> Client:
        gh_app = web.Application()
        gh_app.router.add_route("*", "/{tail:.*}", fake_github.handler)
        gh_client = await aiohttp_client(gh_app)
        api_url = str(gh_client.make_url(""))
        fake_github.base_url = api_url.rstrip("/")

        session = github_session("test-pat", api_url)
        app = web.Application()
        app[GATE] = Gate(session, PolicyEngine(rules), AuditLog(audit_path))
        aiohttp_mcp_server.setup(app, name="gh-mcp-test", tools=aiohttp_mcp_server.ToolRegistry(*ALL_TOOLS))

        async def github_client(app: web.Application) -> AsyncIterator[None]:
            async with session:
                yield

        app.cleanup_ctx.append(github_client)
        return await aiohttp_client(app)

    return maker


async def call_tool(
    client: Client, name: str, arguments: dict[str, object]
) -> dict[str, Any]:
    """Invoke a tool through the real MCP endpoint, returning the call result."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": name,
            "arguments": arguments,
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
                "io.modelcontextprotocol/clientCapabilities": {},
            },
        },
    }
    headers = {
        "Accept": "application/json, text/event-stream",
        "Mcp-Method": "tools/call",
        "Mcp-Name": name,
        "MCP-Protocol-Version": PROTOCOL_VERSION,
    }
    async with client.post("/mcp", json=body, headers=headers) as resp:
        assert resp.status == 200, await resp.text()
        envelope = await resp.json()
    assert "result" in envelope, envelope
    return cast(dict[str, Any], envelope["result"])


def tool_error(result: dict[str, Any]) -> str:
    assert result.get("isError"), result
    return cast(str, result["content"][0]["text"])


EXAMPLE = Path(__file__).parent.parent / "example-config.toml"
TEST_SECRET = "ab" * 32
TEST_PASSWORD = "a long random password"


@pytest.fixture
def example_config_file(tmp_path: Path) -> Path:
    """The example config with its intentionally-unparseable values filled in.

    The example file leaves ``secret`` and ``user1`` without values (invalid
    TOML) so an insecure example credential can never be copied into a real
    deployment; tests substitute known values to validate the rest.
    """
    text = EXAMPLE.read_text()
    text = text.replace("secret =  #", f'secret = "{TEST_SECRET}"  #')
    text = text.replace("user1 =  #", f'user1 = "{TEST_PASSWORD}"  #')
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path

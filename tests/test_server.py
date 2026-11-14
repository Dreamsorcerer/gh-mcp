from pathlib import Path

import pytest
from conftest import TEST_PASSWORD
from pytest_aiohttp import AiohttpClient

from gh_mcp.config import load_config
from gh_mcp.server import build_app


@pytest.fixture
def example_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)  # audit.jsonl goes to a tmp dir


async def test_build_app_requires_auth(
    aiohttp_client: AiohttpClient, example_env: None, example_config_file: Path
) -> None:
    app = build_app(load_config(example_config_file))
    client = await aiohttp_client(app)
    resp = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert resp.status == 401
    assert "Bearer" in resp.headers["WWW-Authenticate"]


async def test_build_app_serves_oauth_metadata(
    aiohttp_client: AiohttpClient, example_env: None, example_config_file: Path
) -> None:
    app = build_app(load_config(example_config_file))
    client = await aiohttp_client(app)
    resp = await client.get("/.well-known/oauth-protected-resource/mcp")
    assert resp.status == 200
    body = await resp.json()
    assert body["resource"] == "https://mcp.example.org/mcp"


async def test_login_submit_checks_credentials(
    aiohttp_client: AiohttpClient, example_env: None, example_config_file: Path
) -> None:
    # Exercises the real OAuth sub-application routing: _check_credentials
    # must read USERS from the root app via config_dict (not request.app).
    app = build_app(load_config(example_config_file))
    client = await aiohttp_client(app)

    resp = await client.post(
        "/oauth2/login",
        data={"username": "user1", "password": TEST_PASSWORD},
        allow_redirects=False,
    )
    assert resp.status == 303
    assert "__Host-aiohttp_mcp_server_session" in resp.headers.get("Set-Cookie", "") or resp.cookies

    resp = await client.post(
        "/oauth2/login", data={"username": "user1", "password": "wrong"}
    )
    assert resp.status == 200
    assert "Invalid credentials" in await resp.text()

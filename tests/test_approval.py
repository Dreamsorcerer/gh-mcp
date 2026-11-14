"""Per-call user approval (``effect = "elicit"``) for write tools.

The flow under test: a write matching an elicit rule returns an
``input_required`` result carrying a URL elicitation; the user opens the
single-use link, is identified as the same user the agent is connected as
(login required), and submits the approval form; the client's retry then
performs the write under the rule's constraints.
"""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from conftest import (
    AGENT_TOKEN,
    BROWSER_USER_HDR,
    PROTOCOL_VERSION,
    Client,
    FakeGitHub,
    MakeApp,
    call_tool,
    tool_error,
)
from yarl import URL

from gh_mcp.config import Constraints, Rule

URL_CAPS: dict[str, object] = {"elicitation": {"form": {}, "url": {}}}
BROWSER = {BROWSER_USER_HDR: "sam"}

ELICIT_PR = Rule(
    actions=["pr:create", "pr:update", "repo:fork"],
    effect="elicit",
    constraints=Constraints(draft_only=True, title_prefix="[agent] "),
)
PR_ARGS: dict[str, object] = {
    "repo": "octo/hello",
    "title": "Fix leak",
    "body": "Details",
    "head": "sam:fix",
    "base": "main",
}


@pytest.fixture(autouse=True)
def _no_form_wait() -> Iterator[None]:
    """Fail fast if a test ever hits the submission-wait path."""
    with mock.patch("aiohttp_mcp_server._elicitation._FORM_WAIT", 0):
        yield


def audit_entries(audit_path: Path) -> list[dict[str, object]]:
    if not audit_path.exists():
        return []
    return [json.loads(line) for line in audit_path.read_text().splitlines()]


async def start_approval(
    client: Client, name: str, arguments: dict[str, object]
) -> tuple[str, str]:
    """First attempt: returns the approval page path and the request state."""
    result = await call_tool(
        client, name, arguments, token=AGENT_TOKEN, capabilities=URL_CAPS
    )
    assert result.get("resultType") == "input_required", result
    params = result["inputRequests"]["approval"]["params"]
    assert params["mode"] == "url"
    path = URL(params["url"]).path
    assert path.startswith("/mcp/elicit/")
    return path, result["requestState"]


async def retry(
    client: Client, name: str, arguments: dict[str, object], state: str
) -> dict[str, Any]:
    """Resubmit after the client accepted the URL elicitation."""
    return await call_tool(
        client,
        name,
        arguments,
        token=AGENT_TOKEN,
        capabilities=URL_CAPS,
        input_responses={"approval": {"action": "accept"}},
        request_state=state,
    )


async def test_pr_approved_in_browser(
    make_app: MakeApp, fake_github: FakeGitHub, audit_path: Path
) -> None:
    fake_github.add(
        "POST",
        "/repos/octo/hello/pulls",
        {"number": 3, "html_url": "https://github.com/octo/hello/pull/3", "draft": True},
        status=201,
    )
    client = await make_app([ELICIT_PR], auth=True)
    path, state = await start_approval(client, "create_pr", PR_ARGS)
    assert not fake_github.requests  # Nothing happens before approval.

    # The link itself grants nothing: login is required, and only the user
    # the agent is connected as may approve.
    async with client.get(path) as page:
        assert page.status == 401
    async with client.get(path, headers={BROWSER_USER_HDR: "mallory"}) as page:
        assert page.status == 403

    async with client.get(path, headers=BROWSER) as page:
        assert page.status == 200
        body = await page.text()
    # The default page HTML-escapes the message, so the quotes are entities.
    assert "open pull request &quot;Fix leak&quot; from sam:fix into main" in body
    assert "octo/hello" in body
    assert "connected as sam" in body

    async with client.post(path, data={"approve": "on"}, headers=BROWSER) as resp:
        assert "return to your agent" in await resp.text()

    result = await retry(client, "create_pr", PR_ARGS, state)
    assert result["structuredContent"] == {
        "number": 3,
        "url": "https://github.com/octo/hello/pull/3",
        "draft": True,
    }
    payload = fake_github.requests[0][3]
    assert payload == {
        "title": "[agent] Fix leak",  # The elicit rule's constraints apply.
        "body": "Details",
        "head": "sam:fix",
        "base": "main",
        "draft": True,
    }
    (entry,) = audit_entries(audit_path)
    assert entry["decision"] == "allow"
    assert entry["detail"] == "approved by the user"
    assert entry["url"] == "https://github.com/octo/hello/pull/3"


async def test_fork_approved_in_browser(
    make_app: MakeApp, fake_github: FakeGitHub
) -> None:
    fake_github.add(
        "POST",
        "/repos/octo/hello/forks",
        {
            "full_name": "sam/hello",
            "default_branch": "main",
            "html_url": "https://github.com/sam/hello",
        },
        status=202,
    )
    client = await make_app([ELICIT_PR], auth=True)
    args: dict[str, object] = {"repo": "octo/hello"}
    path, state = await start_approval(client, "create_fork", args)

    async with client.get(path, headers=BROWSER) as page:
        assert "fork the repository in octo/hello" in await page.text()
    async with client.post(path, data={"approve": "on"}, headers=BROWSER):
        pass

    result = await retry(client, "create_fork", args, state)
    assert result["structuredContent"]["full_name"] == "sam/hello"
    assert fake_github.requests[0][:2] == ("POST", "/repos/octo/hello/forks")


async def test_submit_without_approve_denies(
    make_app: MakeApp, fake_github: FakeGitHub, audit_path: Path
) -> None:
    client = await make_app([ELICIT_PR], auth=True)
    path, state = await start_approval(client, "create_pr", PR_ARGS)

    async with client.post(path, data={}, headers=BROWSER) as resp:
        assert resp.status == 200

    result = await retry(client, "create_pr", PR_ARGS, state)
    assert "was not approved by the user" in tool_error(result)
    assert not fake_github.requests
    (entry,) = audit_entries(audit_path)
    assert entry["decision"] == "deny"


async def test_client_decline_denies(
    make_app: MakeApp, fake_github: FakeGitHub, audit_path: Path
) -> None:
    client = await make_app([ELICIT_PR], auth=True)
    _path, state = await start_approval(client, "create_pr", PR_ARGS)

    result = await call_tool(
        client,
        "create_pr",
        PR_ARGS,
        token=AGENT_TOKEN,
        capabilities=URL_CAPS,
        input_responses={"approval": {"action": "decline"}},
        request_state=state,
    )
    assert "was not approved by the user" in tool_error(result)
    assert not fake_github.requests
    (entry,) = audit_entries(audit_path)
    assert entry["decision"] == "deny"


async def test_client_without_url_elicitation_is_spec_error(
    make_app: MakeApp, fake_github: FakeGitHub
) -> None:
    """The library rejects url elicitation for incapable clients itself."""
    client = await make_app([ELICIT_PR], auth=True)
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "create_pr",
            "arguments": PR_ARGS,
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
                "io.modelcontextprotocol/clientCapabilities": {},
            },
        },
    }
    headers = {
        "Accept": "application/json, text/event-stream",
        "Mcp-Method": "tools/call",
        "Mcp-Name": "create_pr",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
        "Authorization": f"Bearer {AGENT_TOKEN}",
    }
    async with client.post("/mcp", json=body, headers=headers) as resp:
        assert resp.status == 400
        error = (await resp.json())["error"]
    assert error["code"] == -32021  # MISSING_CLIENT_CAPABILITY
    assert error["data"]["requiredCapabilities"] == {"elicitation": {"url": {}}}
    assert not fake_github.requests


async def test_without_authenticated_user_denies(
    make_app: MakeApp, fake_github: FakeGitHub
) -> None:
    client = await make_app([ELICIT_PR])
    result = await call_tool(client, "create_pr", PR_ARGS, capabilities=URL_CAPS)
    assert "requires an authenticated user" in tool_error(result)
    assert not fake_github.requests


async def test_deny_rule_beats_elicit(
    make_app: MakeApp, fake_github: FakeGitHub
) -> None:
    deny = Rule(actions=["pr:create"], repos=["octo/*"], effect="deny")
    client = await make_app([deny, ELICIT_PR], auth=True)
    result = await call_tool(
        client, "create_pr", PR_ARGS, token=AGENT_TOKEN, capabilities=URL_CAPS
    )
    assert "denied by policy" in tool_error(result)
    assert not fake_github.requests


async def test_read_cannot_elicit(make_app: MakeApp, fake_github: FakeGitHub) -> None:
    client = await make_app([Rule(actions=["pr:*"], effect="elicit")], auth=True)
    result = await call_tool(
        client,
        "list_prs",
        {"repo": "octo/hello"},
        token=AGENT_TOKEN,
        capabilities=URL_CAPS,
    )
    assert "which read tools cannot request" in tool_error(result)
    assert not fake_github.requests


async def test_empty_update_rejected_before_approval(
    make_app: MakeApp, fake_github: FakeGitHub
) -> None:
    client = await make_app([ELICIT_PR], auth=True)
    result = await call_tool(
        client,
        "update_pr",
        {"repo": "octo/hello", "number": 3},
        token=AGENT_TOKEN,
        capabilities=URL_CAPS,
    )
    assert "Nothing to update" in tool_error(result)
    assert not fake_github.requests

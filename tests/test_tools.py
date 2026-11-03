import base64
import json
from pathlib import Path

from aiohttp import web
from conftest import FakeGitHub, MakeApp, call_tool, tool_error
from pytest_aiohttp import AiohttpClient

from gh_mcp.config import Constraints, Rule

READ_ALL = Rule(actions=["*:read"])


def audit_entries(audit_path: Path) -> list[dict[str, object]]:
    if not audit_path.exists():
        return []
    return [json.loads(line) for line in audit_path.read_text().splitlines()]


async def test_get_repo(make_app: MakeApp, fake_github: FakeGitHub) -> None:
    fake_github.add(
        "GET",
        "/repos/aio-libs/aiohttp",
        {
            "full_name": "aio-libs/aiohttp",
            "description": "HTTP client/server",
            "default_branch": "master",
            "private": False,
            "html_url": "https://github.com/aio-libs/aiohttp",
        },
    )
    client = await make_app([READ_ALL])
    result = await call_tool(client, "get_repo", {"repo": "aio-libs/aiohttp"})
    assert result["structuredContent"]["default_branch"] == "master"
    # The PAT went to GitHub, not the client.
    *_, headers = fake_github.requests[0]
    assert headers["Authorization"] == "Bearer test-pat"


async def test_get_file_decodes_base64(make_app: MakeApp, fake_github: FakeGitHub) -> None:
    fake_github.add(
        "GET",
        "/repos/o/r/contents/setup.py",
        {
            "type": "file",
            "path": "setup.py",
            "sha": "abc",
            "size": 5,
            "content": base64.b64encode(b"print('hi')\n").decode(),
        },
    )
    client = await make_app([READ_ALL])
    result = await call_tool(
        client, "get_file", {"repo": "o/r", "path": "setup.py"}
    )
    assert result["structuredContent"]["content"] == "print('hi')\n"


async def test_get_pr_diff_uses_diff_accept(
    make_app: MakeApp, fake_github: FakeGitHub
) -> None:
    fake_github.add("GET", "/repos/o/r/pulls/1", "diff --git a/x b/x\n")
    client = await make_app([READ_ALL])
    result = await call_tool(
        client, "get_pr_diff", {"repo": "o/r", "number": 1}
    )
    assert result["content"][0]["text"].startswith("diff --git")
    assert fake_github.requests[0][4]["Accept"] == "application/vnd.github.diff"


async def test_read_denied_without_rule(make_app: MakeApp, fake_github: FakeGitHub) -> None:
    client = await make_app([Rule(actions=["*:read"], repos=["aio-libs/*"])])
    result = await call_tool(client, "get_repo", {"repo": "evil/repo"})
    assert "not permitted for evil/repo" in tool_error(result)
    assert not fake_github.requests  # never reached GitHub


async def test_create_pr_constraints(
    make_app: MakeApp, fake_github: FakeGitHub, audit_path: Path
) -> None:
    fake_github.add(
        "POST",
        "/repos/aio-libs/aiohttp/pulls",
        {"number": 7, "html_url": "https://github.com/aio-libs/aiohttp/pull/7", "draft": True},
        status=201,
    )
    rule = Rule(
        actions=["pr:create"],
        repos=["aio-libs/*"],
        constraints=Constraints(
            draft_only=True,
            title_prefix="[agent] ",
            body_prefix="Agent PR.\n\n",
        ),
    )
    client = await make_app([rule])
    result = await call_tool(
        client,
        "create_pr",
        {
            "repo": "aio-libs/aiohttp",
            "title": "Fix leak",
            "body": "Details",
            "head": "me:fix",
            "base": "master",
        },
    )
    assert result["structuredContent"] == {
        "number": 7,
        "url": "https://github.com/aio-libs/aiohttp/pull/7",
        "draft": True,
    }
    payload = fake_github.requests[0][3]
    assert payload == {
        "title": "[agent] Fix leak",
        "body": "Agent PR.\n\nDetails",
        "head": "me:fix",
        "base": "master",
        "draft": True,
    }
    (entry,) = audit_entries(audit_path)
    assert entry["decision"] == "allow"
    assert entry["url"] == "https://github.com/aio-libs/aiohttp/pull/7"
    assert entry["action"] == "pr:create"


async def test_write_denied_is_audited(
    make_app: MakeApp, fake_github: FakeGitHub, audit_path: Path
) -> None:
    client = await make_app([READ_ALL])
    result = await call_tool(
        client,
        "create_pr",
        {"repo": "o/r", "title": "t", "body": "b", "head": "h", "base": "main"},
    )
    assert "pr:create not permitted" in tool_error(result)
    (entry,) = audit_entries(audit_path)
    assert entry["decision"] == "deny"
    assert not fake_github.requests


async def test_failed_write_is_audited(
    make_app: MakeApp, fake_github: FakeGitHub, audit_path: Path
) -> None:
    # No fake route: GitHub answers 404 after policy allowed the write.
    client = await make_app([Rule(actions=["pr:create"], repos=["o/r"])])
    result = await call_tool(
        client,
        "create_pr",
        {"repo": "o/r", "title": "t", "body": "b", "head": "h", "base": "main"},
    )
    assert "HTTP 404" in tool_error(result)
    (entry,) = audit_entries(audit_path)
    assert entry["decision"] == "error"
    assert "HTTP 404" in str(entry["detail"])


async def test_create_issue_labels(make_app: MakeApp, fake_github: FakeGitHub) -> None:
    fake_github.add(
        "POST", "/repos/o/r/issues", {"number": 3, "html_url": "u"}, status=201
    )
    client = await make_app([Rule(actions=["issue:create"], repos=["o/r"])])
    await call_tool(
        client,
        "create_issue",
        {"repo": "o/r", "title": "t", "body": "b", "labels": ["bug", "p1"]},
    )
    payload = fake_github.requests[0][3]
    assert isinstance(payload, dict)
    assert payload["labels"] == ["bug", "p1"]


async def test_add_comment_suffix_idempotent(
    make_app: MakeApp, fake_github: FakeGitHub
) -> None:
    fake_github.add(
        "POST", "/repos/o/r/issues/1/comments", {"html_url": "u"}, status=201
    )
    rule = Rule(
        actions=["issue:comment"],
        repos=["o/r"],
        constraints=Constraints(body_suffix="\n-- agent"),
    )
    client = await make_app([rule])
    await call_tool(
        client,
        "add_comment",
        {"repo": "o/r", "number": 1, "body": "LGTM\n-- agent"},
    )
    assert fake_github.requests[0][3] == {"body": "LGTM\n-- agent"}


async def test_gh_api_get_path_policy(make_app: MakeApp, fake_github: FakeGitHub) -> None:
    fake_github.add("GET", "/repos/o/r/branches", [{"name": "main"}])
    rule = Rule(
        actions=["raw:read"],
        repos=["o/r"],
        constraints=Constraints(paths=["/repos/o/r/*"]),
    )
    client = await make_app([rule])
    result = await call_tool(client, "gh_api_get", {"path": "/repos/o/r/branches"})
    assert not result.get("isError")

    denied = await call_tool(client, "gh_api_get", {"path": "/user"})
    assert "not permitted" in tool_error(denied)
    assert len(fake_github.requests) == 1


async def test_gh_api_get_text_response(make_app: MakeApp, fake_github: FakeGitHub) -> None:
    # Some endpoints redirect to plain text (e.g. job logs on blob storage).
    fake_github.add("GET", "/repos/o/r/actions/jobs/1/logs", "line 1\nline 2")
    client = await make_app([Rule(actions=["raw:read"])])
    result = await call_tool(
        client, "gh_api_get", {"path": "/repos/o/r/actions/jobs/1/logs"}
    )
    assert result["structuredContent"] == {"result": "line 1\nline 2"}


async def test_gh_api_get_rejects_relative_path(make_app: MakeApp) -> None:
    client = await make_app([READ_ALL, Rule(actions=["raw:read"])])
    result = await call_tool(client, "gh_api_get", {"path": "repos/o/r"})
    assert "absolute GitHub API path" in tool_error(result)
    result = await call_tool(client, "gh_api_get", {"path": "/repos/../user"})
    assert "absolute GitHub API path" in tool_error(result)


async def test_get_job_logs_redirect_without_auth(
    make_app: MakeApp, fake_github: FakeGitHub, aiohttp_client: AiohttpClient
) -> None:
    client = await make_app([READ_ALL])
    log_text = "\n".join(f"line {i}" for i in range(10))
    # The signed-URL host is a different origin (another port), as on GitHub;
    # it must never see the Authorization header.
    seen_headers: dict[str, str] = {}

    async def signed(request: web.Request) -> web.Response:
        seen_headers.update(request.headers)
        return web.Response(text=log_text)

    logs_app = web.Application()
    logs_app.router.add_get("/signed/logs", signed)
    logs_client = await aiohttp_client(logs_app)
    fake_github.add(
        "GET",
        "/repos/o/r/actions/jobs/5/logs",
        "",
        status=302,
        headers={"Location": str(logs_client.make_url("/signed/logs"))},
    )
    result = await call_tool(
        client, "get_job_logs", {"repo": "o/r", "job_id": 5, "tail": 3}
    )
    text = result["content"][0]["text"]
    assert text.splitlines() == ["... (7 earlier lines omitted)", "line 7", "line 8", "line 9"]
    assert seen_headers and "Authorization" not in seen_headers


async def test_search_issues_parses_repository(
    make_app: MakeApp, fake_github: FakeGitHub
) -> None:
    fake_github.add(
        "GET",
        "/search/issues",
        {
            "items": [
                {
                    "repository_url": "https://api.github.com/repos/aio-libs/aiohttp",
                    "number": 42,
                    "title": "Bug",
                    "state": "open",
                    "html_url": "u",
                }
            ]
        },
    )
    client = await make_app([READ_ALL])
    result = await call_tool(client, "search_issues", {"query": "is:open bug"})
    item = result["structuredContent"][0]
    assert item["repository"] == "aio-libs/aiohttp"
    assert item["number"] == 42
    assert item["is_pull_request"] is False


async def test_malformed_repo_rejected(make_app: MakeApp, fake_github: FakeGitHub) -> None:
    client = await make_app([READ_ALL])
    result = await call_tool(client, "get_repo", {"repo": "no-owner"})
    assert "Invalid arguments" in tool_error(result)
    result = await call_tool(client, "get_repo", {"repo": "a/b/c"})
    assert "Invalid arguments" in tool_error(result)
    assert not fake_github.requests

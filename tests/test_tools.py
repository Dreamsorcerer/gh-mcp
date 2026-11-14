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
    # The PAT went to GitHub, not the client.
    *_, headers = fake_github.requests[0]
    assert headers["Authorization"] == "Bearer test-pat"


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
    result = await call_tool(client, "get_file", {"repo": "evil/repo", "path": "x"})
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


async def test_create_fork(
    make_app: MakeApp, fake_github: FakeGitHub, audit_path: Path
) -> None:
    fake_github.add(
        "POST",
        "/repos/aio-libs/aiohttp/forks",
        {
            "full_name": "me/aiohttp",
            "default_branch": "master",
            "html_url": "https://github.com/me/aiohttp",
        },
        status=202,
    )
    client = await make_app([Rule(actions=["repo:fork"], repos=["aio-libs/*"])])
    result = await call_tool(client, "create_fork", {"repo": "aio-libs/aiohttp"})
    assert result["structuredContent"] == {
        "full_name": "me/aiohttp",
        "default_branch": "master",
        "url": "https://github.com/me/aiohttp",
    }
    (entry,) = audit_entries(audit_path)
    assert entry["action"] == "repo:fork"
    assert entry["decision"] == "allow"
    assert entry["url"] == "https://github.com/me/aiohttp"


async def test_create_fork_denied(make_app: MakeApp, fake_github: FakeGitHub) -> None:
    client = await make_app([READ_ALL])
    result = await call_tool(client, "create_fork", {"repo": "o/r"})
    assert "repo:fork not permitted" in tool_error(result)
    assert not fake_github.requests



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

    denied = await call_tool(client, "gh_api_get", {"path": "/repos/o/other/branches"})
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


async def test_gh_api_get_repo_scoped_only(
    make_app: MakeApp, fake_github: FakeGitHub
) -> None:
    # Even the broadest rule cannot reach account- or org-level endpoints.
    client = await make_app([READ_ALL, Rule(actions=["raw:read"])])
    for path in ("/user", "/user/repos", "/orgs/aio-libs/members", "/repos/incomplete"):
        result = await call_tool(client, "gh_api_get", {"path": path})
        assert "only repository paths" in tool_error(result), path
    assert not fake_github.requests


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


JOB_LOG = "\n".join(f"line {i}" for i in range(1, 21))


async def test_get_job_logs_full(make_app: MakeApp, fake_github: FakeGitHub) -> None:
    fake_github.add("GET", "/repos/o/r/actions/jobs/9/logs", JOB_LOG)
    client = await make_app([READ_ALL])
    result = await call_tool(
        client, "get_job_logs", {"repo": "o/r", "job_id": 9, "tail": 0}
    )
    assert result["content"][0]["text"] == JOB_LOG


async def test_get_job_logs_offset(make_app: MakeApp, fake_github: FakeGitHub) -> None:
    fake_github.add("GET", "/repos/o/r/actions/jobs/9/logs", JOB_LOG)
    client = await make_app([READ_ALL])
    result = await call_tool(
        client, "get_job_logs", {"repo": "o/r", "job_id": 9, "offset": 5, "limit": 3}
    )
    assert result["content"][0]["text"].splitlines() == [
        "... (4 earlier lines omitted)",
        "line 5",
        "line 6",
        "line 7",
        "... (13 later lines omitted)",
    ]
    past_end = await call_tool(
        client, "get_job_logs", {"repo": "o/r", "job_id": 9, "offset": 100}
    )
    assert "past the end" in tool_error(past_end)


async def test_get_job_logs_grep(make_app: MakeApp, fake_github: FakeGitHub) -> None:
    fake_github.add("GET", "/repos/o/r/actions/jobs/9/logs", JOB_LOG)
    client = await make_app([READ_ALL])
    result = await call_tool(
        client, "get_job_logs", {"repo": "o/r", "job_id": 9, "grep": "line (1|7)$"}
    )
    assert result["content"][0]["text"] == "1:line 1\n7:line 7"

    capped = await call_tool(
        client, "get_job_logs", {"repo": "o/r", "job_id": 9, "grep": "line", "limit": 2}
    )
    assert capped["content"][0]["text"].splitlines() == [
        "1:line 1",
        "2:line 2",
        "... (18 more matching lines omitted)",
    ]

    none = await call_tool(
        client, "get_job_logs", {"repo": "o/r", "job_id": 9, "grep": "zzz"}
    )
    assert none["content"][0]["text"] == "No matches in 20 log lines."

    bad = await call_tool(
        client, "get_job_logs", {"repo": "o/r", "job_id": 9, "grep": "("}
    )
    assert "Invalid grep regex" in tool_error(bad)


async def test_list_workflow_runs_filters(
    make_app: MakeApp, fake_github: FakeGitHub
) -> None:
    fake_github.add(
        "GET",
        "/repos/o/r/actions/runs",
        {
            "workflow_runs": [
                {
                    "id": 1,
                    "name": "CI",
                    "head_branch": "main",
                    "event": "push",
                    "status": "completed",
                    "conclusion": "failure",
                    "created_at": "2026-11-03T04:05:06Z",
                    "html_url": "u",
                }
            ]
        },
    )
    client = await make_app([READ_ALL])
    result = await call_tool(
        client,
        "list_workflow_runs",
        {"repo": "o/r", "status": "failure", "created": ">=2026-11-01",
         "limit": 2, "page": 3},
    )
    run = result["structuredContent"][0]
    assert run["conclusion"] == "failure"
    assert run["created_at"] == "2026-11-03T04:05:06Z"
    query = fake_github.requests[0][2]
    assert query == {
        "status": "failure",
        "created": ">=2026-11-01",
        "per_page": "2",
        "page": "3",
    }


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


async def test_list_releases_prunes_assets(
    make_app: MakeApp, fake_github: FakeGitHub
) -> None:
    # A real release carries hundreds of asset objects and a full changelog;
    # the tool must return only the four identifying fields.
    fake_github.add(
        "GET",
        "/repos/o/r/releases",
        [
            {
                "tag_name": "v1.1",
                "name": "1.1",
                "published_at": "2026-01-02T00:00:00Z",
                "html_url": "https://github.com/o/r/releases/tag/v1.1",
                "body": "changelog " * 1000,
                "assets": [{"name": f"w{n}.whl"} for n in range(300)],
            },
            {
                "tag_name": "v1.0",
                "name": "1.0",
                "published_at": "2026-01-01T00:00:00Z",
                "html_url": "https://github.com/o/r/releases/tag/v1.0",
                "body": "",
                "assets": [],
            },
        ],
    )
    client = await make_app([READ_ALL])
    result = await call_tool(client, "list_releases", {"repo": "o/r"})
    items = result["structuredContent"]
    assert [r["tag"] for r in items] == ["v1.1", "v1.0"]
    assert set(items[0]) == {"tag", "name", "published_at", "url"}


async def test_malformed_repo_rejected(make_app: MakeApp, fake_github: FakeGitHub) -> None:
    client = await make_app([READ_ALL])
    result = await call_tool(client, "get_file", {"repo": "no-owner", "path": "x"})
    assert "Invalid arguments" in tool_error(result)
    result = await call_tool(client, "get_file", {"repo": "a/b/c", "path": "x"})
    assert "Invalid arguments" in tool_error(result)
    assert not fake_github.requests


async def test_update_advisory(
    make_app: MakeApp, fake_github: FakeGitHub, audit_path: Path
) -> None:
    adv_path = "/repos/o/r/security-advisories/GHSA-xxxx-yyyy-zzzz"
    fake_github.add("GET", adv_path, {"state": "draft"})
    fake_github.add(
        "PATCH",
        adv_path,
        {
            "ghsa_id": "GHSA-xxxx-yyyy-zzzz",
            "summary": "Oops",
            "description": "Details",
            "severity": "high",
            "state": "closed",
            "cve_id": None,
            "created_at": "2026-10-01T00:00:00Z",
            "published_at": None,
            "closed_at": "2026-11-01T00:00:00Z",
            "credits": None,
            "html_url": "https://github.com/o/r/security/advisories/GHSA-xxxx-yyyy-zzzz",
        },
    )
    client = await make_app([Rule(actions=["ghsa:update"], repos=["o/r"])])
    result = await call_tool(
        client,
        "update_advisory",
        {
            "repo": "o/r",
            "ghsa_id": "GHSA-xxxx-yyyy-zzzz",
            "summary": "Oops",
            "severity": "high",
            "state": "closed",
        },
    )
    assert result["structuredContent"]["state"] == "closed"
    method, _, _, payload, _ = fake_github.requests[1]
    assert method == "PATCH"
    assert payload == {"summary": "Oops", "severity": "high", "state": "closed"}
    (entry,) = audit_entries(audit_path)
    assert entry["action"] == "ghsa:update"
    assert entry["decision"] == "allow"


async def test_update_advisory_published_refused(
    make_app: MakeApp, fake_github: FakeGitHub, audit_path: Path
) -> None:
    adv_path = "/repos/o/r/security-advisories/GHSA-pppp-qqqq-rrrr"
    fake_github.add("GET", adv_path, {"state": "published"})
    client = await make_app([Rule(actions=["ghsa:update"], repos=["o/r"])])
    result = await call_tool(
        client,
        "update_advisory",
        {"repo": "o/r", "ghsa_id": "GHSA-pppp-qqqq-rrrr", "summary": "s"},
    )
    assert "only unpublished advisories" in tool_error(result)
    assert [req[0] for req in fake_github.requests] == ["GET"]  # never patched
    (entry,) = audit_entries(audit_path)
    assert entry["decision"] == "error"


async def test_update_advisory_cannot_publish(
    make_app: MakeApp, fake_github: FakeGitHub
) -> None:
    client = await make_app([Rule(actions=["ghsa:update"])])
    # 'published' is not in the state schema; publishing is impossible.
    result = await call_tool(
        client,
        "update_advisory",
        {"repo": "o/r", "ghsa_id": "GHSA-x", "state": "published"},
    )
    assert "Invalid arguments" in tool_error(result)

    empty = await call_tool(
        client, "update_advisory", {"repo": "o/r", "ghsa_id": "GHSA-x"}
    )
    assert "Nothing to update" in tool_error(empty)
    assert not fake_github.requests


async def test_update_advisory_full_fields(
    make_app: MakeApp, fake_github: FakeGitHub
) -> None:
    adv_path = "/repos/o/r/security-advisories/GHSA-xxxx-yyyy-zzzz"
    fake_github.add("GET", adv_path, {"state": "triage"})
    fake_github.add(
        "PATCH",
        adv_path,
        {
            "ghsa_id": "GHSA-xxxx-yyyy-zzzz",
            "summary": "s",
            "description": "d",
            "severity": None,
            "state": "draft",
            "cve_id": "CVE-2026-1234",
            "created_at": "2026-10-01T00:00:00Z",
            "published_at": None,
            "closed_at": None,
            "credits": [{"login": "iamibi", "type": "reporter"}],
            "html_url": "u",
        },
    )
    client = await make_app([Rule(actions=["ghsa:update"], repos=["o/r"])])
    vulnerability = {
        "package": {"ecosystem": "pip", "name": "aiohttp"},
        "vulnerable_version_range": "<=3.14.3",
        "patched_versions": "3.14.4",
    }
    result = await call_tool(
        client,
        "update_advisory",
        {
            "repo": "o/r",
            "ghsa_id": "GHSA-xxxx-yyyy-zzzz",
            "vulnerabilities": [vulnerability],
            "credits": [{"login": "iamibi", "type": "reporter"}],
            "cvss_vector_string": "CVSS:3.1/AV:N/AC:L",
        },
    )
    assert result["structuredContent"]["state"] == "draft"
    method, _, _, payload, _ = fake_github.requests[1]
    assert method == "PATCH"
    assert payload == {
        "vulnerabilities": [vulnerability],
        "credits": [{"login": "iamibi", "type": "reporter"}],
        "cvss_vector_string": "CVSS:3.1/AV:N/AC:L",
    }


async def test_update_advisory_severity_conflict(
    make_app: MakeApp, fake_github: FakeGitHub
) -> None:
    client = await make_app([Rule(actions=["ghsa:update"], repos=["o/r"])])
    result = await call_tool(
        client,
        "update_advisory",
        {
            "repo": "o/r",
            "ghsa_id": "GHSA-x",
            "severity": "high",
            "cvss_vector_string": "CVSS:3.1/AV:N",
        },
    )
    assert "not both" in tool_error(result)
    assert not fake_github.requests


async def test_list_advisories_filters_and_paging(
    make_app: MakeApp, fake_github: FakeGitHub
) -> None:
    fake_github.add(
        "GET",
        "/repos/o/r/security-advisories",
        [
            {
                "ghsa_id": "GHSA-a",
                "cve_id": None,
                "summary": "s",
                "severity": "low",
                "state": "closed",
                "created_at": "2026-01-01T00:00:00Z",
                "closed_at": "2026-02-01T00:00:00Z",
                "html_url": "u",
            }
        ],
        headers={
            "Link": '<https://x/repos/o/r/security-advisories'
            '?per_page=1&after=cur123>; rel="next"'
        },
    )
    client = await make_app([READ_ALL])
    result = await call_tool(
        client, "list_repo_advisories", {"repo": "o/r", "state": "closed", "limit": 1}
    )
    page = result["structuredContent"]
    assert page["advisories"][0]["closed_at"] == "2026-02-01T00:00:00Z"
    assert page["next_cursor"] == "cur123"
    assert fake_github.requests[0][2] == {"state": "closed", "per_page": "1"}

    # Follow the cursor; the last page has no next Link.
    fake_github.add("GET", "/repos/o/r/security-advisories", [])
    result = await call_tool(
        client,
        "list_repo_advisories",
        {"repo": "o/r", "state": "closed", "limit": 1, "after": "cur123"},
    )
    assert result["structuredContent"] == {"advisories": [], "next_cursor": None}
    assert fake_github.requests[1][2] == {
        "state": "closed",
        "per_page": "1",
        "after": "cur123",
    }

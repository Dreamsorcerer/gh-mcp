from pathlib import Path

from conftest import FakeGitHub, MakeApp, call_tool, tool_error
from test_tools import audit_entries

from gh_mcp.config import Constraints, Rule

GIT_RULE = Rule(
    actions=["git:*"],
    repos=["o/r"],
    constraints=Constraints(branch_prefix="agent/", message_prefix="[agent] "),
)


def add_git_routes(gh: FakeGitHub) -> None:
    gh.add("GET", "/repos/o/r", {"default_branch": "main"})
    gh.add("GET", "/repos/o/r/git/ref/heads/main", {"object": {"sha": "base000"}})
    gh.add("GET", "/repos/o/r/git/ref/heads/agent/fix", {"object": {"sha": "head111"}})
    gh.add("POST", "/repos/o/r/git/refs", {"ref": "refs/heads/agent/fix"}, status=201)
    gh.add("GET", "/repos/o/r/git/commits/head111", {"tree": {"sha": "tree222"}})
    gh.add("POST", "/repos/o/r/git/blobs", {"sha": "blob333"}, status=201)
    gh.add("POST", "/repos/o/r/git/trees", {"sha": "tree444"}, status=201)
    gh.add(
        "POST",
        "/repos/o/r/git/commits",
        {"sha": "commit555", "html_url": "https://github.com/o/r/commit/commit555"},
        status=201,
    )
    gh.add(
        "PATCH", "/repos/o/r/git/refs/heads/agent/fix", {"object": {"sha": "commit555"}}
    )


async def test_create_branch_applies_prefix(
    make_app: MakeApp, fake_github: FakeGitHub
) -> None:
    add_git_routes(fake_github)
    client = await make_app([GIT_RULE])
    result = await call_tool(client, "create_branch", {"repo": "o/r", "branch": "fix"})
    assert result["structuredContent"] == {
        "branch": "agent/fix",
        "sha": "base000",
        "url": "https://github.com/o/r/tree/agent/fix",
    }
    # Default branch resolved, then the ref created from its head.
    posted = next(r for r in fake_github.requests if r[0] == "POST")
    assert posted[3] == {"ref": "refs/heads/agent/fix", "sha": "base000"}


async def test_create_branch_explicit_source_and_idempotent_prefix(
    make_app: MakeApp, fake_github: FakeGitHub
) -> None:
    add_git_routes(fake_github)
    fake_github.add(
        "GET", "/repos/o/r/git/ref/heads/dev", {"object": {"sha": "dev999"}}
    )
    client = await make_app([GIT_RULE])
    result = await call_tool(
        client,
        "create_branch",
        {"repo": "o/r", "branch": "agent/fix", "from_branch": "dev"},
    )
    assert result["structuredContent"]["branch"] == "agent/fix"  # not doubled
    posted = next(r for r in fake_github.requests if r[0] == "POST")
    assert posted[3] == {"ref": "refs/heads/agent/fix", "sha": "dev999"}
    # The default branch was never looked up.
    assert not any(r[1] == "/repos/o/r" for r in fake_github.requests)


async def test_commit_files(
    make_app: MakeApp, fake_github: FakeGitHub, audit_path: Path
) -> None:
    add_git_routes(fake_github)
    client = await make_app([GIT_RULE])
    result = await call_tool(
        client,
        "commit_files",
        {
            "repo": "o/r",
            "branch": "fix",
            "message": "Fix the leak",
            "files": [
                {"path": "src/a.py", "content": "print('hi')\n"},
                {"path": "old.txt"},
            ],
        },
    )
    assert result["structuredContent"] == {
        "sha": "commit555",
        "branch": "agent/fix",
        "url": "https://github.com/o/r/commit/commit555",
    }
    by_path = {r[1]: r for r in fake_github.requests if r[0] == "POST"}
    assert by_path["/repos/o/r/git/blobs"][3] == {
        "content": "print('hi')\n",
        "encoding": "utf-8",
    }
    assert by_path["/repos/o/r/git/trees"][3] == {
        "base_tree": "tree222",
        "tree": [
            {"path": "src/a.py", "mode": "100644", "type": "blob", "sha": "blob333"},
            {"path": "old.txt", "mode": "100644", "type": "blob", "sha": None},
        ],
    }
    assert by_path["/repos/o/r/git/commits"][3] == {
        "message": "[agent] Fix the leak",
        "tree": "tree444",
        "parents": ["head111"],
    }
    patched = next(r for r in fake_github.requests if r[0] == "PATCH")
    assert patched[3] == {"sha": "commit555"}  # no force key, ever
    (entry,) = audit_entries(audit_path)
    assert entry["action"] == "git:commit"
    assert entry["url"] == "https://github.com/o/r/commit/commit555"


async def test_commit_files_non_fast_forward(
    make_app: MakeApp, fake_github: FakeGitHub, audit_path: Path
) -> None:
    add_git_routes(fake_github)
    fake_github.add(
        "PATCH",
        "/repos/o/r/git/refs/heads/agent/fix",
        {"message": "Update is not a fast forward"},
        status=422,
    )
    client = await make_app([GIT_RULE])
    result = await call_tool(
        client,
        "commit_files",
        {
            "repo": "o/r",
            "branch": "fix",
            "message": "m",
            "files": [{"path": "a", "content": "b"}],
        },
    )
    assert "fast forward" in tool_error(result)
    (entry,) = audit_entries(audit_path)
    assert entry["decision"] == "error"


async def test_commit_files_requires_files(
    make_app: MakeApp, fake_github: FakeGitHub
) -> None:
    client = await make_app([GIT_RULE])
    result = await call_tool(
        client,
        "commit_files",
        {"repo": "o/r", "branch": "fix", "message": "m", "files": []},
    )
    assert "Invalid arguments" in tool_error(result)
    assert not fake_github.requests


async def test_git_denied_without_rule(make_app: MakeApp, fake_github: FakeGitHub) -> None:
    client = await make_app([Rule(actions=["*:read"])])
    result = await call_tool(client, "create_branch", {"repo": "o/r", "branch": "fix"})
    assert "git:branch not permitted" in tool_error(result)
    assert not fake_github.requests

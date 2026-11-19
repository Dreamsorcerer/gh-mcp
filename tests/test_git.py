from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
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
    gh.add(
        "GET",
        "/repos/o/r/git/trees/tree222",
        {"tree": [{"path": "src", "mode": "040000", "type": "tree", "sha": "srctree"}]},
    )
    gh.add(
        "GET",
        "/repos/o/r/git/trees/srctree",
        {"tree": [{"path": "a.py", "mode": "100644", "type": "blob", "sha": "olda"}]},
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
    # The existing file's mode was looked up by walking its parent trees;
    # the deletion needed no lookup.
    looked_up = [r[1] for r in fake_github.requests if "/git/trees/" in r[1]]
    assert looked_up == ["/repos/o/r/git/trees/tree222", "/repos/o/r/git/trees/srctree"]
    (entry,) = audit_entries(audit_path)
    assert entry["action"] == "git:commit"
    assert entry["url"] == "https://github.com/o/r/commit/commit555"


async def test_commit_files_preserves_executable_mode(
    make_app: MakeApp, fake_github: FakeGitHub
) -> None:
    """An update keeps 100755; a symlink is replaced by a regular file."""
    add_git_routes(fake_github)
    fake_github.add(
        "GET",
        "/repos/o/r/git/trees/tree222",
        {
            "tree": [
                {"path": "build.sh", "mode": "100755", "type": "blob", "sha": "old1"},
                {"path": "link", "mode": "120000", "type": "blob", "sha": "old2"},
            ]
        },
    )
    client = await make_app([GIT_RULE])
    await call_tool(
        client,
        "commit_files",
        {
            "repo": "o/r",
            "branch": "fix",
            "message": "m",
            "files": [
                {"path": "build.sh", "content": "#!/bin/sh\n"},
                {"path": "link", "content": "not a symlink"},
            ],
        },
    )
    tree = next(r for r in fake_github.requests if r[1] == "/repos/o/r/git/trees")
    assert tree[3] == {
        "base_tree": "tree222",
        "tree": [
            {"path": "build.sh", "mode": "100755", "type": "blob", "sha": "blob333"},
            {"path": "link", "mode": "100644", "type": "blob", "sha": "blob333"},
        ],
    }
    # Both files sit in the root directory: one listing serves both.
    looked_up = [r[1] for r in fake_github.requests if "/git/trees/" in r[1]]
    assert looked_up == ["/repos/o/r/git/trees/tree222"]


async def test_commit_files_explicit_mode_skips_lookup(
    make_app: MakeApp, fake_github: FakeGitHub
) -> None:
    add_git_routes(fake_github)
    client = await make_app([GIT_RULE])
    await call_tool(
        client,
        "commit_files",
        {
            "repo": "o/r",
            "branch": "fix",
            "message": "m",
            "files": [{"path": "tools/run.sh", "content": "x", "mode": "100755"}],
        },
    )
    tree = next(r for r in fake_github.requests if r[1] == "/repos/o/r/git/trees")
    assert tree[3] == {
        "base_tree": "tree222",
        "tree": [
            {"path": "tools/run.sh", "mode": "100755", "type": "blob", "sha": "blob333"}
        ],
    }
    assert not any("/git/trees/" in r[1] for r in fake_github.requests)


async def test_commit_files_new_paths_default_mode(
    make_app: MakeApp, fake_github: FakeGitHub
) -> None:
    """Paths absent from the base tree become 100644, walking no further than needed."""
    add_git_routes(fake_github)
    fake_github.add(
        "GET",
        "/repos/o/r/git/trees/tree222",
        {
            "tree": [
                {"path": "src", "mode": "040000", "type": "tree", "sha": "srctree"},
                {"path": "README", "mode": "100644", "type": "blob", "sha": "readme1"},
            ]
        },
    )
    client = await make_app([GIT_RULE])
    await call_tool(
        client,
        "commit_files",
        {
            "repo": "o/r",
            "branch": "fix",
            "message": "m",
            "files": [
                {"path": "new/a.txt", "content": "a"},  # no new/ directory
                {"path": "src/new.py", "content": "b"},  # dir exists, file doesn't
                {"path": "README/x", "content": "c"},  # README is a file, not a dir
            ],
        },
    )
    tree = next(r for r in fake_github.requests if r[1] == "/repos/o/r/git/trees")
    assert tree[3] == {
        "base_tree": "tree222",
        "tree": [
            {"path": "new/a.txt", "mode": "100644", "type": "blob", "sha": "blob333"},
            {"path": "src/new.py", "mode": "100644", "type": "blob", "sha": "blob333"},
            {"path": "README/x", "mode": "100644", "type": "blob", "sha": "blob333"},
        ],
    }
    looked_up = [r[1] for r in fake_github.requests if "/git/trees/" in r[1]]
    assert looked_up == ["/repos/o/r/git/trees/tree222", "/repos/o/r/git/trees/srctree"]


async def test_commit_files_rejects_bad_mode(
    make_app: MakeApp, fake_github: FakeGitHub
) -> None:
    client = await make_app([GIT_RULE])
    result = await call_tool(
        client,
        "commit_files",
        {
            "repo": "o/r",
            "branch": "fix",
            "message": "m",
            "files": [{"path": "a", "content": "b", "mode": "100777"}],
        },
    )
    assert "Invalid arguments" in tool_error(result)
    assert not fake_github.requests


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


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        # Each would otherwise reach heads/main, past the forced prefix.
        ("commit_files", {"branch": "x/../../main"}),
        ("commit_files", {"branch": "x/%2e%2e/%2e%2e/main"}),
        ("commit_files", {"branch": "../main?"}),
        ("commit_files", {"branch": "..\\main#"}),
        ("create_branch", {"branch": "fix", "from_branch": "../../../../x/y/git/ref/heads/z"}),
        ("merge_branch", {"branch": "x/./../../main"}),
        # Another repository's branch, in GitHub's comparison syntax.
        ("merge_branch", {"branch": "fix", "from_branch": "other:main"}),
    ],
)
async def test_branch_names_cannot_redirect_requests(
    make_app: MakeApp, fake_github: FakeGitHub, tool: str, arguments: dict[str, object]
) -> None:
    add_git_routes(fake_github)
    client = await make_app([GIT_RULE])
    if tool == "commit_files":
        arguments = {**arguments, "message": "m", "files": [{"path": "a", "content": "b"}]}
    result = await call_tool(client, tool, {"repo": "o/r", **arguments})
    assert "Invalid arguments" in tool_error(result)
    assert not fake_github.requests


def add_tree(gh: FakeGitHub, sha: str, files: Mapping[str, tuple[str, str]]) -> None:
    """Serve the tree ``sha`` holding ``files`` (path -> (mode, blob sha))."""
    entries = []
    subtrees: dict[str, dict[str, tuple[str, str]]] = {}
    for path, (mode, blob) in files.items():
        name, _, rest = path.partition("/")
        if rest:
            subtrees.setdefault(name, {})[rest] = (mode, blob)
        else:
            entries.append({"path": name, "mode": mode, "type": "blob", "sha": blob})
    for name, subtree in subtrees.items():
        entries.append(
            {"path": name, "mode": "040000", "type": "tree", "sha": f"{sha}-{name}"}
        )
        add_tree(gh, f"{sha}-{name}", subtree)
    gh.add("GET", f"/repos/o/r/git/trees/{sha}", {"tree": entries})


# What the trees hold, by path: (mode, sha).
Files = Mapping[str, tuple[str, str]]

# agent/fix (head111, tree222) and main (main999, theirtree) since they
# diverged at fork000 (basetree).
MERGE_BASE = {
    "ci.yml": ("100644", "ci0"),
    "old.txt": ("100644", "old0"),
    "run.sh": ("100644", "run0"),
    "same.txt": ("100644", "same0"),
    "src/a.py": ("100644", "a0"),
    "src/x.py": ("100644", "x0"),
}
BRANCH = {
    "ci.yml": ("100644", "ci1"),  # main changed it too
    # old.txt deleted
    "run.sh": ("100644", "run1"),  # main changed it too, and made it executable
    "same.txt": ("100644", "same1"),  # main made the same change
    "src/a.py": ("100755", "a1"),
    "src/y.py": ("100644", "x0"),  # renamed from src/x.py, which main changed
}
MAIN = {
    "ci.yml": ("100644", "ci2"),
    "old.txt": ("100644", "old0"),
    "run.sh": ("100755", "run2"),
    "same.txt": ("100644", "same1"),
    "src/a.py": ("100644", "a0"),
    "src/x.py": ("100644", "x2"),
    "src/main_only.py": ("100644", "m2"),
}
BRANCH_CHANGES = [
    {"filename": "ci.yml", "status": "modified"},
    {"filename": "old.txt", "status": "removed"},
    {"filename": "run.sh", "status": "modified"},
    {"filename": "same.txt", "status": "modified"},
    {"filename": "src/a.py", "status": "modified"},
    {"filename": "src/y.py", "status": "renamed", "previous_filename": "src/x.py"},
]
RESOLUTIONS = [
    {"path": "ci.yml", "take": "from_branch"},
    {"path": "run.sh", "content": "merged\n"},
    {"path": "src/y.py", "content": "moved and merged\n"},
    {"path": "src/x.py", "take": "branch"},
]


def add_merge_routes(
    gh: FakeGitHub,
    changes: list[dict[str, str]] = BRANCH_CHANGES,
    *,
    base: Files = MERGE_BASE,
    branch: Files = BRANCH,
    main: Files = MAIN,
) -> None:
    """Two branches GitHub cannot merge itself; ``changes`` is its list of agent/fix's."""
    add_git_routes(gh)
    gh.add("POST", "/repos/o/r/merges", {"message": "Merge conflict"}, status=409)
    gh.add(
        "GET",
        "/repos/o/r/compare/main...head111",
        {
            "behind_by": 3,
            "base_commit": {"sha": "main999", "commit": {"tree": {"sha": "theirtree"}}},
            "merge_base_commit": {"sha": "fork000", "commit": {"tree": {"sha": "basetree"}}},
            "files": changes,
        },
    )
    add_tree(gh, "basetree", base)
    add_tree(gh, "tree222", branch)
    add_tree(gh, "theirtree", main)


def writes(gh: FakeGitHub) -> list[tuple[str, str]]:
    return [(r[0], r[1]) for r in gh.requests if r[0] != "GET"]


async def test_merge_branch_by_github(
    make_app: MakeApp, fake_github: FakeGitHub, audit_path: Path
) -> None:
    add_git_routes(fake_github)
    fake_github.add(
        "POST",
        "/repos/o/r/merges",
        {"sha": "merge777", "html_url": "https://github.com/o/r/commit/merge777"},
        status=201,
    )
    client = await make_app([GIT_RULE])
    result = await call_tool(client, "merge_branch", {"repo": "o/r", "branch": "fix"})
    assert result["structuredContent"] == {
        "sha": "merge777",
        "branch": "agent/fix",
        "url": "https://github.com/o/r/commit/merge777",
    }
    # The default branch, merged into the prefixed one.
    assert fake_github.requests[-1][3] == {
        "base": "agent/fix",
        "head": "main",
        "commit_message": "[agent] Merge main into agent/fix",
    }
    (entry,) = audit_entries(audit_path)
    assert (entry["tool"], entry["action"]) == ("merge_branch", "git:commit")
    assert entry["url"] == "https://github.com/o/r/commit/merge777"


async def test_merge_branch_already_up_to_date(
    make_app: MakeApp, fake_github: FakeGitHub
) -> None:
    add_git_routes(fake_github)
    fake_github.add("POST", "/repos/o/r/merges", status=204)
    fake_github.add("GET", "/repos/o/r/compare/dev...head111", {"behind_by": 0})
    client = await make_app([GIT_RULE])
    result = await call_tool(
        client, "merge_branch", {"repo": "o/r", "branch": "fix", "from_branch": "dev"}
    )
    assert result["structuredContent"] == {
        "sha": "head111",
        "branch": "agent/fix",
        "url": "https://github.com/o/r/tree/agent/fix",
    }
    assert writes(fake_github) == [("POST", "/repos/o/r/merges")]


async def test_merge_branch_conflict_names_files(
    make_app: MakeApp, fake_github: FakeGitHub, audit_path: Path
) -> None:
    add_merge_routes(fake_github)
    client = await make_app([GIT_RULE])
    result = await call_tool(client, "merge_branch", {"repo": "o/r", "branch": "fix"})
    error = tool_error(result)
    # A rename conflicts as a whole when main changed the file under its old
    # name; each side lacks one of the two names.
    assert (
        "agent/fix and main both changed ci.yml, run.sh, src/y.py (not in main),"
        " src/x.py (not in agent/fix)." in error
    )
    # The three versions the agent has to merge.
    assert "ref head111 (agent/fix), main999 (main) or fork000 (their merge base)" in error
    # Nothing was written.
    assert writes(fake_github) == [("POST", "/repos/o/r/merges")]
    (entry,) = audit_entries(audit_path)
    assert entry["decision"] == "error"


async def test_merge_branch_with_resolutions(
    make_app: MakeApp, fake_github: FakeGitHub
) -> None:
    add_merge_routes(fake_github)
    client = await make_app([GIT_RULE])
    result = await call_tool(
        client,
        "merge_branch",
        {"repo": "o/r", "branch": "fix", "from_branch": "main", "resolutions": RESOLUTIONS},
    )
    assert result["structuredContent"] == {
        "sha": "commit555",
        "branch": "agent/fix",
        "url": "https://github.com/o/r/commit/commit555",
    }
    # GitHub is not asked to merge what it cannot: built from main's tree.
    posts = [r for r in fake_github.requests if r[0] == "POST"]
    assert "/repos/o/r/merges" not in [r[1] for r in posts]
    (tree,) = (r[3] for r in posts if r[1] == "/repos/o/r/git/trees")
    assert tree == {
        "base_tree": "theirtree",
        "tree": [
            # Only the branch changed these: its versions carry over.
            {"path": "old.txt", "mode": "100644", "type": "blob", "sha": None},
            {"path": "src/a.py", "mode": "100755", "type": "blob", "sha": "a1"},
            # Resolved by taking the branch's side: the old name stays gone.
            {"path": "src/x.py", "mode": "100644", "type": "blob", "sha": None},
            # Resolved with content; run.sh keeps the mode main gave it.
            {"path": "run.sh", "mode": "100755", "type": "blob", "sha": "blob333"},
            {"path": "src/y.py", "mode": "100644", "type": "blob", "sha": "blob333"},
            # ci.yml (main's side taken) and same.txt are as main has them.
        ],
    }
    commit = next(r[3] for r in posts if r[1] == "/repos/o/r/git/commits")
    assert commit == {
        "message": "[agent] Merge main into agent/fix",
        "tree": "tree444",
        "parents": ["head111", "main999"],
    }
    assert fake_github.requests[-1][:2] == ("PATCH", "/repos/o/r/git/refs/heads/agent/fix")
    assert fake_github.requests[-1][3] == {"sha": "commit555"}


@pytest.mark.parametrize(
    ("resolutions", "error"),
    [
        (RESOLUTIONS[1:], "both changed ci.yml."),
        ([*RESOLUTIONS, {"path": "src/a.py", "take": "from_branch"}], "Not in conflict: src/a.py"),
        ([{"path": "ci.yml"}], "ci.yml needs either content or take"),
        ([{"path": "ci.yml", "content": "x", "take": "branch"}], "ci.yml needs either"),
    ],
)
async def test_merge_branch_rejects_resolutions_not_matching_conflicts(
    make_app: MakeApp,
    fake_github: FakeGitHub,
    resolutions: list[dict[str, str]],
    error: str,
) -> None:
    add_merge_routes(fake_github)
    client = await make_app([GIT_RULE])
    result = await call_tool(
        client, "merge_branch", {"repo": "o/r", "branch": "fix", "resolutions": resolutions}
    )
    assert error in tool_error(result)
    assert not {
        ("POST", "/repos/o/r/git/commits"),
        ("PATCH", "/repos/o/r/git/refs/heads/agent/fix"),
    } & set(writes(fake_github))


async def test_merge_branch_refuses_more_changes_than_github_lists(
    make_app: MakeApp, fake_github: FakeGitHub
) -> None:
    add_merge_routes(fake_github, [{"filename": f"f{n}", "status": "added"} for n in range(300)])
    client = await make_app([GIT_RULE])
    result = await call_tool(client, "merge_branch", {"repo": "o/r", "branch": "fix"})
    assert "too many files" in tool_error(result)
    assert writes(fake_github) == [("POST", "/repos/o/r/merges")]


FILE = "100644"


def written(path: str, sha: str | None, mode: str = FILE, kind: str = "blob") -> dict[str, object]:
    """A tree entry as the merge writes it; no ``sha`` removes the file."""
    return {"path": path, "mode": mode, "type": kind, "sha": sha}


# The merge base, agent/fix and main, with GitHub's list of what agent/fix
# changed; then the entries the merge writes onto main's tree, or its error.
THREE_WAY: dict[
    str, tuple[Files, Files, Files, list[dict[str, str]], list[dict[str, object]] | str]
] = {
    "added in a new directory": (
        {"a": (FILE, "a0")},
        {"a": (FILE, "a0"), "new/pkg/x": (FILE, "x1")},
        {"a": (FILE, "a2")},
        [{"filename": "new/pkg/x", "status": "added"}],
        [written("new/pkg/x", "x1")],
    ),
    "a directory deleted whole": (
        {"a": (FILE, "a0"), "old/x": (FILE, "x0"), "old/sub/y": (FILE, "y0")},
        {"a": (FILE, "a0")},
        {"a": (FILE, "a2"), "old/x": (FILE, "x0"), "old/sub/y": (FILE, "y0")},
        [
            {"filename": "old/sub/y", "status": "removed"},
            {"filename": "old/x", "status": "removed"},
        ],
        [written("old/sub/y", None), written("old/x", None)],
    ),
    "renamed into a new directory": (
        {"a.py": (FILE, "a0")},
        {"pkg/a.py": (FILE, "a0")},
        {"a.py": (FILE, "a0"), "m": (FILE, "m2")},
        [{"filename": "pkg/a.py", "status": "renamed", "previous_filename": "a.py"}],
        [written("pkg/a.py", "a0"), written("a.py", None)],
    ),
    "made executable": (
        {"run": (FILE, "r0")},
        {"run": ("100755", "r0")},
        {"run": (FILE, "r0"), "m": (FILE, "m2")},
        [{"filename": "run", "status": "changed"}],
        [written("run", "r0", "100755")],
    ),
    "submodule moved on": (
        {"lib": ("160000", "c0")},
        {"lib": ("160000", "c1")},
        {"lib": ("160000", "c0"), "m": (FILE, "m2")},
        [{"filename": "lib", "status": "modified"}],
        [written("lib", "c1", "160000", "commit")],
    ),
    "changed the same on both": (
        {"f": (FILE, "f0")},
        {"f": (FILE, "f1")},
        {"f": (FILE, "f1"), "m": (FILE, "m2")},
        [{"filename": "f", "status": "modified"}],
        [],
    ),
    "changed here, deleted on main": (
        {"f": (FILE, "f0"), "k": (FILE, "k0")},
        {"f": (FILE, "f1"), "k": (FILE, "k0")},
        {"k": (FILE, "k0")},
        [{"filename": "f", "status": "modified"}],
        "both changed f (not in main).",
    ),
    "deleted here, changed on main": (
        {"f": (FILE, "f0"), "k": (FILE, "k0")},
        {"k": (FILE, "k0")},
        {"f": (FILE, "f2"), "k": (FILE, "k0")},
        [{"filename": "f", "status": "removed"}],
        "both changed f (not in agent/fix).",
    ),
    "added differently on both": (
        {"k": (FILE, "k0")},
        {"k": (FILE, "k0"), "n": (FILE, "n1")},
        {"k": (FILE, "k0"), "n": (FILE, "n2")},
        [{"filename": "n", "status": "added"}],
        "both changed n.",
    ),
    # Written there, the file would displace all that main's directory holds.
    "a file where main has a directory": (
        {"k": (FILE, "k0")},
        {"k": (FILE, "k0"), "thing": (FILE, "t1")},
        {"k": (FILE, "k0"), "thing/inner": (FILE, "i2")},
        [{"filename": "thing", "status": "added"}],
        "where agent/fix has the file thing, main has a directory",
    ),
    "a file under what main has as a file": (
        {"k": (FILE, "k0")},
        {"k": (FILE, "k0"), "thing/inner": (FILE, "i1")},
        {"k": (FILE, "k0"), "thing": (FILE, "t2")},
        [{"filename": "thing/inner", "status": "added"}],
        "where agent/fix has the file thing/inner, main has a directory",
    ),
    # Merged regardless, a change GitHub left out would be silently undone.
    "a change unlisted beside a listed one": (
        {"src/a": (FILE, "a0"), "src/b": (FILE, "b0")},
        {"src/a": (FILE, "a1"), "src/b": (FILE, "b1")},
        {"src/a": (FILE, "a0"), "src/b": (FILE, "b0"), "m": (FILE, "m2")},
        [{"filename": "src/a", "status": "modified"}],
        "list of the files agent/fix has changed is incomplete",
    ),
    "a change unlisted in another directory": (
        {"a": (FILE, "a0"), "lib/deep/z": (FILE, "z0")},
        {"a": (FILE, "a1"), "lib/deep/z": (FILE, "z1")},
        {"a": (FILE, "a0"), "lib/deep/z": (FILE, "z0"), "m": (FILE, "m2")},
        [{"filename": "a", "status": "modified"}],
        "list of the files agent/fix has changed is incomplete",
    ),
    "changes with none listed": (
        {"a": (FILE, "a0")},
        {"a": (FILE, "a1")},
        {"a": (FILE, "a0"), "m": (FILE, "m2")},
        [],
        "list of the files agent/fix has changed is incomplete",
    ),
}


@pytest.mark.parametrize("case", THREE_WAY.values(), ids=THREE_WAY.keys())
async def test_merge_branch_three_way(
    make_app: MakeApp,
    fake_github: FakeGitHub,
    case: tuple[Files, Files, Files, list[dict[str, str]], list[dict[str, object]] | str],
) -> None:
    base, branch, main, changes, outcome = case
    add_merge_routes(fake_github, changes, base=base, branch=branch, main=main)
    client = await make_app([GIT_RULE])
    result = await call_tool(client, "merge_branch", {"repo": "o/r", "branch": "fix"})
    if isinstance(outcome, str):
        assert outcome in tool_error(result)
        assert writes(fake_github) == [("POST", "/repos/o/r/merges")]
        return
    assert not result.get("isError"), result
    posts: dict[str, Any] = {r[1]: r[3] for r in fake_github.requests if r[0] == "POST"}
    if outcome:
        assert posts["/repos/o/r/git/trees"] == {"base_tree": "theirtree", "tree": outcome}
    else:
        # Nothing of the branch's to add: the merge has main's tree as it is.
        assert "/repos/o/r/git/trees" not in posts
    assert posts["/repos/o/r/git/commits"] == {
        "message": "[agent] Merge main into agent/fix",
        "tree": "tree444" if outcome else "theirtree",
        "parents": ["head111", "main999"],
    }

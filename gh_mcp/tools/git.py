"""Git tools: branches and commits via the Git Data API."""

from typing import Annotated, NotRequired, TypedDict

from aiohttp_mcp import Doc, MCPRequest
from pydantic import Field

from ..policy import apply_prefix
from ._base import GATE, Repo


class GitRef(TypedDict):
    """A branch tip: the branch name, its commit sha, and a browse URL."""

    branch: str
    sha: str
    url: str


class FileEdit(TypedDict):
    path: str
    # Omit to delete the file.
    content: NotRequired[str]


async def create_branch(
    ctx: MCPRequest, repo: Repo, branch: str, from_branch: str | None = None
) -> GitRef:
    """Create a branch from another branch's head (default branch if omitted)."""
    gate = ctx.http.app[GATE]
    async with gate.write(ctx, "create_branch", "git:branch", repo) as write:
        branch = apply_prefix(branch, write.constraints.get("branch_prefix", ""))
        if from_branch is None:
            async with gate.gh.get(f"/repos/{repo}") as resp:
                from_branch = (await resp.json())["default_branch"]
        async with gate.gh.get(f"/repos/{repo}/git/ref/heads/{from_branch}") as resp:
            sha = (await resp.json())["object"]["sha"]
        payload = {"ref": f"refs/heads/{branch}", "sha": sha}
        async with gate.gh.post(f"/repos/{repo}/git/refs", json=payload) as resp:
            await resp.json()
        write.url = f"https://github.com/{repo}/tree/{branch}"
    return {"branch": branch, "sha": sha, "url": write.url}


async def commit_files(
    ctx: MCPRequest,
    repo: Repo,
    branch: str,
    message: Annotated[str, Doc("One line summary of changes")],
    files: Annotated[list[FileEdit], Field(min_length=1)],
) -> GitRef:
    """Create a commit on a branch."""
    gate = ctx.http.app[GATE]

    async with gate.write(ctx, "commit_files", "git:commit", repo) as write:
        branch = apply_prefix(branch, write.constraints.get("branch_prefix", ""))
        message = apply_prefix(message, write.constraints.get("message_prefix", ""))

        async with gate.gh.get(f"/repos/{repo}/git/ref/heads/{branch}") as resp:
            head = (await resp.json())["object"]["sha"]
        async with gate.gh.get(f"/repos/{repo}/git/commits/{head}") as resp:
            base_tree = (await resp.json())["tree"]["sha"]

        entries = []
        for file in files:
            sha = None
            if (content := file.get("content")) is not None:
                blob = {"content": content, "encoding": "utf-8"}
                async with gate.gh.post(f"/repos/{repo}/git/blobs", json=blob) as resp:
                    sha = (await resp.json())["sha"]
            entries.append(
                {"path": file["path"], "mode": "100644", "type": "blob", "sha": sha}
            )
        tree = {"base_tree": base_tree, "tree": entries}
        async with gate.gh.post(f"/repos/{repo}/git/trees", json=tree) as resp:
            tree_sha = (await resp.json())["sha"]

        commit = {"message": message, "tree": tree_sha, "parents": [head]}
        async with gate.gh.post(f"/repos/{repo}/git/commits", json=commit) as resp:
            data = await resp.json()
        sha, write.url = data["sha"], data["html_url"]

        async with gate.gh.patch(
            f"/repos/{repo}/git/refs/heads/{branch}", json={"sha": sha}
        ) as resp:
            await resp.json()
    return {"sha": sha, "branch": branch, "url": write.url}

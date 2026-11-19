"""Git tools: branches, commits and merges via the Git Data API."""

from collections.abc import Collection, Mapping, Sequence
from typing import Annotated, Final, Literal, NotRequired, TypedDict

from aiohttp import ClientSession
from aiohttp_mcp_server import Doc, MCPRequest, ToolError
from pydantic import Field

from ..github import GitHubError
from ..policy import apply_prefix
from ._base import GATE, Branch, Repo

# GitHub lists at most this many of a comparison's changed files.
_COMPARE_LIMIT: Final = 300
_DIRECTORY: Final = "040000"
_SUBMODULE: Final = "160000"

# A file's (mode, sha) in a tree.
_Entry = tuple[str, str]
# The entries, by name, of the directories read of a tree, by directory path:
# "" for the root, the others ending in "/".
_Listings = dict[str, dict[str, _Entry]]


class GitRef(TypedDict):
    """A branch tip: the branch name, its commit sha, and a browse URL."""

    branch: str
    sha: str
    url: str


class FileEdit(TypedDict):
    path: str
    # Omit to delete the file.
    content: NotRequired[str]
    # Omit to keep an existing file's mode (new files become "100644").
    mode: NotRequired[Literal["100644", "100755"]]


class Resolution(TypedDict):
    """How one conflicting file is settled.

    Give its merged ``content``, or ``take`` it as one of the branches has
    it (removing it if that branch has no such file): "branch" is the one
    merged into, "from_branch" the one merged in.
    """

    path: str
    content: NotRequired[str]
    take: NotRequired[Literal["branch", "from_branch"]]


async def create_branch(
    ctx: MCPRequest, repo: Repo, branch: Branch, from_branch: Branch | None = None
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


async def _tree_entries(
    gh: ClientSession, repo: str, root: str, paths: Sequence[str]
) -> tuple[dict[str, _Entry], _Listings]:
    """The entry of each path that is a file in the tree ``root``.

    Walks one directory listing per level (cached per directory), never a
    recursive dump, which can truncate on large repositories. Those listings
    are returned too: every directory the tree has on the way to a path.
    """
    listings: _Listings = {}

    async def list_dir(prefix: str, sha: str) -> dict[str, _Entry]:
        if (entries := listings.get(prefix)) is None:
            async with gh.get(f"/repos/{repo}/git/trees/{sha}") as resp:
                tree = (await resp.json())["tree"]
            entries = {e["path"]: (e["mode"], e["sha"]) for e in tree}
            listings[prefix] = entries
        return entries

    found: dict[str, _Entry] = {}
    for path in paths:
        prefix, sha = "", root
        *dirs, name = path.split("/")
        for part in dirs:
            entry = (await list_dir(prefix, sha)).get(part)
            if entry is None or entry[0] != _DIRECTORY:
                break  # No such directory: a new file.
            prefix, sha = f"{prefix}{part}/", entry[1]
        else:
            entry = (await list_dir(prefix, sha)).get(name)
            if entry is not None and entry[0] != _DIRECTORY:
                found[path] = entry
    return found, listings


def _differ_elsewhere(paths: Collection[str], before: _Listings, after: _Listings) -> bool:
    """Whether two trees differ anywhere but at ``paths``.

    Going by their listings on the way to those paths: any other difference
    shows there as a differing entry that is neither one of the paths nor a
    directory leading to one.
    """
    for prefix in before.keys() | after.keys():
        old, new = before.get(prefix, {}), after.get(prefix, {})
        for name in old.keys() | new.keys():
            if old.get(name) == new.get(name):
                continue
            path = prefix + name
            for entry, listings in ((old.get(name), before), (new.get(name), after)):
                if entry is None:
                    continue
                if entry[0] == _DIRECTORY:
                    if f"{path}/" not in listings:
                        return True
                elif path not in paths:
                    return True
    return False


def _obstructed(path: str, listings: _Listings) -> bool:
    """Whether a tree, going by its listings, has something in a file's way.

    That is a directory at ``path``, or a file where one of the path's
    directories would be: a file written there displaces what that holds.
    """
    prefix = ""
    *directories, name = path.split("/")
    for part in directories:
        entry = listings[prefix].get(part)
        if entry is None:
            return False  # Nothing there at all.
        if entry[0] != _DIRECTORY:
            return True
        prefix = f"{prefix}{part}/"
    entry = listings[prefix].get(name)
    return entry is not None and entry[0] == _DIRECTORY


async def _blob_entries(
    gh: ClientSession,
    repo: str,
    files: Sequence[FileEdit],
    modes: Mapping[str, str | None],
) -> list[dict[str, object]]:
    """Tree entries applying ``files``, uploading the content of each.

    ``modes`` gives the current mode of the files that have one; an edit
    keeps an executable executable unless it sets a mode itself.
    """
    entries: list[dict[str, object]] = []
    for file in files:
        sha = None
        if (content := file.get("content")) is not None:
            blob = {"content": content, "encoding": "utf-8"}
            async with gh.post(f"/repos/{repo}/git/blobs", json=blob) as resp:
                sha = (await resp.json())["sha"]
        default = "100755" if modes.get(file["path"]) == "100755" else "100644"
        entries.append(
            {
                "path": file["path"],
                "mode": file.get("mode", default),
                "type": "blob",
                "sha": sha,
            }
        )
    return entries


async def _write_tree(
    gh: ClientSession, repo: str, base: str, entries: list[dict[str, object]]
) -> str:
    """The sha of the tree ``base`` with ``entries`` applied to it."""
    if not entries:
        return base  # Nothing to change, which GitHub would reject.
    payload = {"base_tree": base, "tree": entries}
    async with gh.post(f"/repos/{repo}/git/trees", json=payload) as resp:
        sha: str = (await resp.json())["sha"]
    return sha


async def _commit(
    gh: ClientSession, repo: str, branch: str, message: str, tree: str, parents: list[str]
) -> tuple[str, str]:
    """Commit ``tree`` onto ``branch``: the new commit's sha and URL."""
    commit = {"message": message, "tree": tree, "parents": parents}
    async with gh.post(f"/repos/{repo}/git/commits", json=commit) as resp:
        data = await resp.json()
    # Never forced: GitHub refuses unless the commit descends from the
    # branch's current head.
    async with gh.patch(
        f"/repos/{repo}/git/refs/heads/{branch}", json={"sha": data["sha"]}
    ) as resp:
        await resp.json()
    return data["sha"], data["html_url"]


async def commit_files(
    ctx: MCPRequest,
    repo: Repo,
    branch: Branch,
    message: Annotated[str, Doc("One line summary of changes")],
    files: Annotated[list[FileEdit], Field(min_length=1)],
) -> GitRef:
    """Create a commit on a branch.

    Each entry replaces the file at ``path`` with ``content`` (omit
    ``content`` to delete the file). Existing files keep their current
    mode unless ``mode`` is set, new files are created as "100644".
    """
    gate = ctx.http.app[GATE]

    async with gate.write(ctx, "commit_files", "git:commit", repo) as write:
        branch = apply_prefix(branch, write.constraints.get("branch_prefix", ""))
        message = apply_prefix(message, write.constraints.get("message_prefix", ""))

        async with gate.gh.get(f"/repos/{repo}/git/ref/heads/{branch}") as resp:
            head = (await resp.json())["object"]["sha"]
        async with gate.gh.get(f"/repos/{repo}/git/commits/{head}") as resp:
            base_tree = (await resp.json())["tree"]["sha"]

        lookups = [f["path"] for f in files if "content" in f and "mode" not in f]
        current, _ = await _tree_entries(gate.gh, repo, base_tree, lookups)
        modes = {path: mode for path, (mode, _) in current.items()}
        entries = await _blob_entries(gate.gh, repo, files, modes)
        tree = await _write_tree(gate.gh, repo, base_tree, entries)
        sha, write.url = await _commit(gate.gh, repo, branch, message, tree, [head])
    return {"sha": sha, "branch": branch, "url": write.url}


def _existing(path: str, entry: _Entry | None) -> dict[str, object]:
    """The tree entry putting ``entry`` at ``path``, or removing it if ``None``."""
    if entry is None:
        return {"path": path, "mode": "100644", "type": "blob", "sha": None}
    mode, sha = entry
    kind = "commit" if mode == _SUBMODULE else "blob"
    return {"path": path, "mode": mode, "type": kind, "sha": sha}


def _merged_mode(
    base: _Entry | None, ours: _Entry | None, theirs: _Entry | None
) -> str | None:
    """The mode of a file merged from these versions: the side changing it wins."""
    was, mine, their = (entry and entry[0] for entry in (base, ours, theirs))
    return (mine if their == was else their) or mine or their


async def _github_merge(
    gh: ClientSession, repo: str, branch: str, source: str, message: str
) -> tuple[str, str] | None:
    """Have GitHub merge ``source`` into ``branch``: the commit's sha and URL.

    ``None`` if GitHub made no commit, either because the branch already
    contains ``source`` or because the two conflict.
    """
    payload = {"base": branch, "head": source, "commit_message": message}
    try:
        async with gh.post(f"/repos/{repo}/merges", json=payload) as resp:
            if resp.status == 204:
                return None
            data = await resp.json()
    except GitHubError as exc:
        if exc.status != 409:
            raise
        return None
    return data["sha"], data["html_url"]


async def _build_merge(
    gh: ClientSession,
    repo: str,
    branch: str,
    source: str,
    message: str,
    resolutions: Sequence[Resolution],
) -> tuple[str, str]:
    """Merge ``source`` into ``branch`` with a merge commit built here.

    A three-way merge of whole files: starting from ``source``'s tree, each
    file only the branch has changed since their merge base takes the
    branch's version. A file both changed, differently, is a conflict for
    ``resolutions`` to settle. Returns the branch's resulting sha and URL.
    """
    async with gh.get(f"/repos/{repo}/git/ref/heads/{branch}") as resp:
        head = (await resp.json())["object"]["sha"]
    # ``source...head`` is the branch's side: its changes since the merge base.
    async with gh.get(
        f"/repos/{repo}/compare/{source}...{head}", params={"per_page": "1"}
    ) as resp:
        comparison = await resp.json()
    if comparison["behind_by"] == 0:
        # Nothing to merge: the branch already contains source.
        return head, f"https://github.com/{repo}/tree/{branch}"
    changed = comparison.get("files", [])
    if len(changed) >= _COMPARE_LIMIT:
        raise ToolError(
            f"{branch} has changed too many files to merge here:"
            f" GitHub lists only the first {_COMPARE_LIMIT}"
        )
    theirs_commit = comparison["base_commit"]
    base_commit = comparison["merge_base_commit"]
    base_tree = base_commit["commit"]["tree"]["sha"]
    theirs_tree = theirs_commit["commit"]["tree"]["sha"]
    async with gh.get(f"/repos/{repo}/git/commits/{head}") as resp:
        ours_tree = (await resp.json())["tree"]["sha"]

    # A rename is one change covering both of its paths; no path is in two.
    changes = [
        [f["filename"], f["previous_filename"]]
        if f["status"] == "renamed"
        else [f["filename"]]
        for f in changed
    ]
    paths = [path for change in changes for path in change]
    base, base_dirs = await _tree_entries(gh, repo, base_tree, paths)
    ours, ours_dirs = await _tree_entries(gh, repo, ours_tree, paths)
    # The merge relies on that list being complete, as a change missing from
    # it would be silently undone: the two trees may differ at those paths
    # alone.
    if base_tree != ours_tree and (
        not paths or _differ_elsewhere(set(paths), base_dirs, ours_dirs)
    ):
        raise ToolError(
            f"Cannot merge here: GitHub's list of the files {branch} has changed"
            " is incomplete"
        )
    theirs, their_dirs = await _tree_entries(gh, repo, theirs_tree, paths)

    kept: list[str] = []
    conflicts: list[str] = []
    for change in changes:
        # Where the branch's version is not what source has anyway...
        mine = [p for p in change if ours.get(p) not in (base.get(p), theirs.get(p))]
        # ...which stands, unless source has changed any of that too.
        if all(theirs.get(p) == base.get(p) for p in mine):
            kept += mine
        else:
            conflicts += mine
    # Nor may anything else of source's be lost: a file of the branch's put
    # where source has a directory, or under what source has as a file, would
    # displace that.
    if clashing := [
        p for p in (*kept, *conflicts) if p in ours and _obstructed(p, their_dirs)
    ]:
        raise ToolError(
            f"Cannot merge here: where {branch} has the file {', '.join(clashing)},"
            f" {source} has a directory, or a file further up the path. Rename or"
            f" delete it on {branch} first."
        )

    settled = {r["path"]: r for r in resolutions}
    if unsettled := [p for p in conflicts if p not in settled]:
        # Worth knowing when settling one: a side may have no such file.
        sides = ((branch, ours), (source, theirs))
        listed = []
        for path in unsettled:
            lacking = [name for name, side in sides if path not in side]
            listed.append(f"{path} (not in {lacking[0]})" if lacking else path)
        raise ToolError(
            f"Merge conflict: {branch} and {source} both changed"
            f" {', '.join(listed)}. Call again with resolutions settling each:"
            " its merged content, or which branch's version to take. To read the"
            f" versions, use get_file with ref {head} ({branch}),"
            f" {theirs_commit['sha']} ({source}) or {base_commit['sha']}"
            " (their merge base)."
        )
    if stray := [p for p in settled if p not in conflicts]:
        raise ToolError(
            f"Not in conflict: {', '.join(stray)}. Resolutions only settle"
            " conflicting files; change others with commit_files."
        )

    edits: list[FileEdit] = []
    modes: dict[str, str | None] = {}
    for path, resolution in settled.items():
        if "content" in resolution:
            edits.append({"path": path, "content": resolution["content"]})
            modes[path] = _merged_mode(base.get(path), ours.get(path), theirs.get(path))
        elif resolution["take"] == "branch":
            kept.append(path)
    entries = [_existing(p, ours.get(p)) for p in kept]
    entries += await _blob_entries(gh, repo, edits, modes)
    tree = await _write_tree(gh, repo, theirs_tree, entries)
    return await _commit(gh, repo, branch, message, tree, [head, theirs_commit["sha"]])


async def merge_branch(
    ctx: MCPRequest,
    repo: Repo,
    branch: Branch,
    from_branch: Branch | None = None,
    resolutions: Annotated[
        list[Resolution] | None,
        Doc("Only after a merge conflict: how to settle each conflicting file"),
    ] = None,
) -> GitRef:
    """Merge another branch into a branch, e.g. to update a pull request from its base.

    Merges ``from_branch`` (default branch if omitted) into ``branch``. If
    the two conflict, the error lists the conflicting files: call again
    with ``resolutions`` settling each of them.
    """
    gate = ctx.http.app[GATE]
    for resolution in resolutions or ():
        if ("content" in resolution) == ("take" in resolution):
            raise ToolError(
                f"The resolution of {resolution['path']} needs either content or take"
            )
    async with gate.write(ctx, "merge_branch", "git:commit", repo) as write:
        constraints = write.constraints
        branch = apply_prefix(branch, constraints.get("branch_prefix", ""))
        if from_branch is None:
            async with gate.gh.get(f"/repos/{repo}") as resp:
                source: str = (await resp.json())["default_branch"]
        else:
            source = from_branch
        message = apply_prefix(
            f"Merge {source} into {branch}", constraints.get("message_prefix", "")
        )
        merged = None
        if not resolutions:
            merged = await _github_merge(gate.gh, repo, branch, source, message)
        if merged is None:
            # Conflicts the caller has resolved, or GitHub made no commit:
            # merging here finds there is nothing to merge, or which files
            # conflict.
            merged = await _build_merge(
                gate.gh, repo, branch, source, message, resolutions or ()
            )
        sha, write.url = merged
    return {"sha": sha, "branch": branch, "url": write.url}

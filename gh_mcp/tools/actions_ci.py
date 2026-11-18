"""GitHub Actions tools (read-only): workflow runs, job logs, artifacts."""

import io
import re
import zipfile
from typing import Annotated, Final, Literal, TypedDict

from aiohttp_mcp_server import Doc, MCPRequest, ToolError
from pydantic import Field

from ..github import paginate
from ._base import GATE, Gate, Repo

# An artifact zip is downloaded whole (zip directories sit at the end, so a
# partial fetch cannot even list it); cap it to keep a runaway artifact from
# exhausting memory. A single entry is capped again before it is decompressed,
# so a small zip cannot expand without bound.
MAX_ARTIFACT_BYTES: Final = 50 * 1024 * 1024
MAX_ENTRY_BYTES: Final = 25 * 1024 * 1024

RunStatus = Literal[
    "completed", "action_required", "cancelled", "failure", "neutral",
    "skipped", "stale", "success", "timed_out", "in_progress", "queued",
    "requested", "waiting", "pending",
]


class RunSummary(TypedDict):
    id: int
    name: str
    head_branch: str
    event: str
    status: str
    conclusion: str | None
    created_at: str
    url: str


class JobSummary(TypedDict):
    id: int
    name: str
    head_branch: str
    status: str
    conclusion: str | None
    created_at: str
    url: str


class Release(TypedDict):
    tag: str
    name: str | None
    published_at: str | None
    url: str


class ArtifactSummary(TypedDict):
    id: int
    name: str
    size_in_bytes: int
    expired: bool
    created_at: str


async def list_workflow_runs(
    ctx: MCPRequest,
    repo: Repo,
    branch: str | None = None,
    status: Annotated[
        RunStatus | None,
        Doc("Only runs with this status or conclusion, e.g. 'failure'"),
    ] = None,
    created: Annotated[
        str | None,
        Doc("Created-date filter, e.g. '>=2026-11-01' or '2026-11-01..2026-11-03'"),
    ] = None,
    limit: int = 20,
    page: Annotated[
        int, Doc("Page of `limit` runs, for paging further back"), Field(ge=1)
    ] = 1,
) -> list[RunSummary]:
    """List recent workflow (CI) runs, newest first."""
    gate = ctx.http.app[GATE]
    gate.check(ctx, "list_workflow_runs", "actions:read", repo)
    params: dict[str, str] = {}
    if branch:
        params["branch"] = branch
    if status:
        params["status"] = status
    if created:
        params["created"] = created
    items = await paginate(
        gate.gh,
        f"/repos/{repo}/actions/runs",
        params=params,
        limit=limit,
        items_key="workflow_runs",
        page=page,
    )
    return [
        {
            "id": run["id"],
            "name": run["name"],
            "head_branch": run["head_branch"],
            "event": run["event"],
            "status": run["status"],
            "conclusion": run["conclusion"],
            "created_at": run["created_at"],
            "url": run["html_url"],
        }
        for run in items
    ]


async def list_run_jobs(
    ctx: MCPRequest, repo: Repo, run_id: int
) -> list[JobSummary]:
    """List the jobs of a workflow run, with their ids for get_job_logs."""
    gate = ctx.http.app[GATE]
    gate.check(ctx, "list_run_jobs", "actions:read", repo)
    items = await paginate(
        gate.gh,
        f"/repos/{repo}/actions/runs/{run_id}/jobs",
        limit=100,
        items_key="jobs",
    )
    return [
        {
            "id": job["id"],
            "name": job["name"],
            "head_branch": job["head_branch"],
            "status": job["status"],
            "conclusion": job["conclusion"],
            "created_at": job["created_at"],
            "url": job["html_url"],
        }
        for job in items
    ]


async def get_job_logs(
    ctx: MCPRequest,
    repo: Repo,
    job_id: int,
    tail: Annotated[
        int, Doc("Last N lines to return; 0 returns the whole log"), Field(ge=0)
    ] = 500,
    offset: Annotated[
        int | None, Doc("1-based line number to read from, instead of tailing"), Field(ge=1)
    ] = None,
    limit: Annotated[
        int, Doc("Max lines returned in offset/grep mode"), Field(ge=1)
    ] = 500,
    grep: Annotated[
        str | None, Doc("Regex: return only matching lines, as 'lineno:text'")
    ] = None,
) -> str:
    """Get the log output of a workflow job."""
    gate = ctx.http.app[GATE]
    gate.check(ctx, "get_job_logs", "actions:read", repo)
    async with gate.gh.get(f"/repos/{repo}/actions/jobs/{job_id}/logs") as resp:
        text = await resp.text()
    return _present_text(text, tail=tail, offset=offset, limit=limit, grep=grep)


def _present_text(
    text: str, *, tail: int, offset: int | None, limit: int, grep: str | None
) -> str:
    """Render log/file text: grep, offset-slice, or tail (in that precedence)."""
    lines = text.splitlines()
    if grep is not None:
        return _grep_log(lines, grep, limit)
    if offset is not None:
        return _slice_log(lines, offset, limit)
    if tail and len(lines) > tail:
        lines = [f"... ({len(lines) - tail} earlier lines omitted)"] + lines[-tail:]
    return "\n".join(lines)


def _grep_log(lines: list[str], pattern: str, limit: int) -> str:
    """Matching lines as ``lineno:text``, capped at ``limit`` matches."""
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise ToolError(f"Invalid grep regex: {exc}") from None
    matches = [f"{n}:{line}" for n, line in enumerate(lines, 1) if regex.search(line)]
    if not matches:
        return f"No matches in {len(lines)} log lines."
    out = matches[:limit]
    if len(matches) > limit:
        out.append(f"... ({len(matches) - limit} more matching lines omitted)")
    return "\n".join(out)


def _slice_log(lines: list[str], offset: int, limit: int) -> str:
    """``limit`` lines from 1-based ``offset``, with omission markers."""
    total = len(lines)
    if offset > total:
        raise ToolError(f"offset {offset} is past the end: the log has {total} lines")
    end = min(offset - 1 + limit, total)
    out = lines[offset - 1:end]
    if offset > 1:
        out.insert(0, f"... ({offset - 1} earlier lines omitted)")
    if end < total:
        out.append(f"... ({total - end} later lines omitted)")
    return "\n".join(out)


async def list_releases(
    ctx: MCPRequest, repo: Repo, limit: int = 10
) -> list[Release]:
    """List a repository's releases, newest first."""
    gate = ctx.http.app[GATE]
    gate.check(ctx, "list_releases", "release:read", repo)
    items = await paginate(gate.gh, f"/repos/{repo}/releases", limit=limit)
    return [
        {
            "tag": release["tag_name"],
            "name": release["name"],
            "published_at": release["published_at"],
            "url": release["html_url"],
        }
        for release in items
    ]


async def list_run_artifacts(
    ctx: MCPRequest, repo: Repo, run_id: int, limit: int = 100
) -> list[ArtifactSummary]:
    """List the artifacts a workflow run uploaded, with ids for read_artifact."""
    gate = ctx.http.app[GATE]
    gate.check(ctx, "list_run_artifacts", "actions:read", repo)
    items = await paginate(
        gate.gh,
        f"/repos/{repo}/actions/runs/{run_id}/artifacts",
        limit=limit,
        items_key="artifacts",
    )
    return [
        {
            "id": art["id"],
            "name": art["name"],
            "size_in_bytes": art["size_in_bytes"],
            "expired": art["expired"],
            "created_at": art["created_at"],
        }
        for art in items
    ]


async def read_artifact(
    ctx: MCPRequest,
    repo: Repo,
    artifact_id: int,
    path: Annotated[
        str | None,
        Doc("File within the artifact zip to read; omit to list the entries"),
    ] = None,
    tail: Annotated[
        int, Doc("Last N lines to return; 0 returns the whole file"), Field(ge=0)
    ] = 500,
    offset: Annotated[
        int | None, Doc("1-based line number to read from, instead of tailing"), Field(ge=1)
    ] = None,
    limit: Annotated[
        int, Doc("Max lines returned in offset/grep mode"), Field(ge=1)
    ] = 500,
    grep: Annotated[
        str | None, Doc("Regex: return only matching lines, as 'lineno:text'")
    ] = None,
) -> str:
    """Read a text file from a run artifact's zip, or list its entries."""
    gate = ctx.http.app[GATE]
    gate.check(ctx, "read_artifact", "actions:read", repo)
    data = await _download(
        gate, f"/repos/{repo}/actions/artifacts/{artifact_id}/zip", "artifact"
    )
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ToolError("the downloaded artifact is not a valid zip archive") from None
    files = [info for info in archive.infolist() if not info.is_dir()]
    if path is None:
        return _list_entries(files)
    try:
        info = archive.getinfo(path)
    except KeyError:
        names = ", ".join(repr(info.filename) for info in files) or "none"
        raise ToolError(f"{path!r} is not in the artifact; entries: {names}") from None
    if info.file_size > MAX_ENTRY_BYTES:
        raise ToolError(
            f"{path!r} is {info.file_size} bytes, over the "
            f"{MAX_ENTRY_BYTES // (1024 * 1024)} MB read limit"
        )
    text = archive.read(info).decode("utf-8", "replace")
    return _present_text(text, tail=tail, offset=offset, limit=limit, grep=grep)


def _list_entries(files: list[zipfile.ZipInfo]) -> str:
    """A listing of an artifact's files and sizes for picking a ``path``."""
    if not files:
        return "The artifact is empty."
    rows = "\n".join(f"{info.file_size:>12}  {info.filename}" for info in files)
    noun = "entry" if len(files) == 1 else "entries"
    return f"{len(files)} {noun} (pass path= to read one):\n{rows}"


async def _download(gate: Gate, path: str, what: str) -> bytes:
    """Fetch ``path`` (following GitHub's redirect to blob storage), size-capped.

    GitHub serves the zip as a 302 to a signed URL on another host; aiohttp
    follows it and drops the Authorization header across that origin boundary.
    """
    chunks: list[bytes] = []
    total = 0
    async with gate.gh.get(path) as resp:
        async for chunk in resp.content.iter_chunked(64 * 1024):
            total += len(chunk)
            if total > MAX_ARTIFACT_BYTES:
                raise ToolError(
                    f"the {what} is larger than the "
                    f"{MAX_ARTIFACT_BYTES // (1024 * 1024)} MB download limit"
                )
            chunks.append(chunk)
    return b"".join(chunks)

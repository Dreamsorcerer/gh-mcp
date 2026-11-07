"""Agent feedback: report missing access or tooling."""


from aiohttp_mcp import MCPRequest

from ._base import GATE, _subject


async def report_missing_access(ctx: MCPRequest, description: str) -> None:
    """Report that access or a tool you need is missing from this server."""
    gate = ctx.http.app[GATE]
    gate.audit.record(
        subject=_subject(ctx),
        tool="report_missing_access",
        action="meta:report",
        resource="",
        decision="report",
        detail=description,
    )

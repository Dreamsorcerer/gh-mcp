"""Shared plumbing for tool implementations."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Final

from aiohttp import ClientSession, web
from aiohttp_mcp_server import Doc, MCPRequest, ToolError
from pydantic import Field

from ..audit import AuditLog
from ..config import Constraints
from ..policy import PolicyEngine

Repo = Annotated[
    str,
    Doc("The repository as owner/name, e.g. 'aio-libs/aiohttp'"),
    Field(pattern=r"^[\w.-]+/[\w.-]+$"),
]


@dataclass(slots=True)
class Write:
    """An in-flight audited write: its constraints and resulting URL."""

    constraints: Constraints
    url: str | None = None


@dataclass(frozen=True, slots=True)
class Gate:
    """Policy check + audit logging wrapper shared by every tool."""

    gh: ClientSession
    policy: PolicyEngine
    audit: AuditLog

    def check(
        self,
        ctx: MCPRequest,
        tool: str,
        action: str,
        resource: str,
        *,
        path: str | None = None,
    ) -> Constraints:
        """Enforce read policy; on deny, audit and raise for the model.

        Writes must go through .write() so their outcome is audited.
        """
        assert action.endswith(":read"), action
        decision = self.policy.check(action, resource, path=path)
        if not decision.allowed:
            self.audit.record(
                subject=_subject(ctx), tool=tool, action=action,
                resource=resource, decision="deny", detail=decision.reason,
            )
            raise ToolError(decision.reason)
        return decision.constraints

    @asynccontextmanager
    async def write(
        self, ctx: MCPRequest, tool: str, action: str, resource: str
    ) -> AsyncIterator[Write]:
        """Run one write under policy, auditing every outcome.

        Deny, failure (ToolError from the GitHub call), and success
        each produce an audit entry; set Write.url inside the block
        so the success entry links to the created object.
        """
        assert not action.endswith(":read"), action
        decision = self.policy.check(action, resource)
        if not decision.allowed:
            self.audit.record(
                subject=_subject(ctx), tool=tool, action=action,
                resource=resource, decision="deny", detail=decision.reason,
            )
            raise ToolError(decision.reason)
        write = Write(decision.constraints)
        try:
            yield write
        except ToolError as exc:
            self.audit.record(
                subject=_subject(ctx), tool=tool, action=action,
                resource=resource, decision="error", detail=str(exc),
            )
            raise
        self.audit.record(
            subject=_subject(ctx), tool=tool, action=action,
            resource=resource, decision="allow", url=write.url,
        )


GATE: Final = web.AppKey("GATE", Gate)


def _subject(ctx: MCPRequest) -> str:
    return "anonymous" if ctx.auth is None else (ctx.auth.subject or "anonymous")



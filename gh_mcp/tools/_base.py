"""Shared plumbing for tool implementations."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import Annotated, Final

from aiohttp import ClientSession, web
from aiohttp_mcp_server import (
    BooleanSchema,
    Doc,
    MCPRequest,
    SecureFormElicitation,
    ToolError,
)
from pydantic import Field

from ..audit import AuditLog
from ..config import Constraints
from ..policy import Decision, PolicyEngine

_APPROVED: Final = "approved by the user"

Repo = Annotated[
    str,
    Doc("The repository as owner/name, e.g. 'aio-libs/aiohttp'"),
    Field(pattern=r"^[\w.-]*[\w-][\w.-]*/[\w.-]*[\w-][\w.-]*$"),
]

# A branch name per git's ref-name rules, and without "#" or "%" either.
_REF_CHAR: Final = r"[^\x00-\x20\x7f~^:?*\[\\#%/.]"
Branch = Annotated[str, Field(pattern=rf"^{_REF_CHAR}+(?:[./]{_REF_CHAR}+)*$")]

GhsaId = Annotated[
    str,
    Doc("The advisory's id, e.g. 'GHSA-xxxx-xxxx-xxxx'"),
    Field(pattern=r"^[A-Za-z0-9-]+$"),
]


def literal_path(path: str) -> bool:
    """Whether a path of several parts is requested just as it reads.

    That takes it having no "." or ".." parts, nor what would leave some once
    read as a URL: a "%" escape, a tab or a line break. Any "?" or "#" is for
    the caller to deal with.
    """
    dropped_or_decoded = {"\t", "\r", "\n", "%"}
    return dropped_or_decoded.isdisjoint(path) and {".", ".."}.isdisjoint(path.split("/"))


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
            reason = decision.reason
            if decision.elicit:
                reason += ", which read tools cannot request"
            self.audit.record(
                subject=_subject(ctx), tool=tool, action=action,
                resource=resource, decision="deny", detail=reason,
            )
            raise ToolError(reason)
        return decision.constraints

    @asynccontextmanager
    async def write(
        self,
        ctx: MCPRequest,
        tool: str,
        action: str,
        resource: str,
        *,
        detail: str | None = None,
    ) -> AsyncIterator[Write]:
        """Run one write under policy, auditing every outcome.

        Deny, failure (ToolError from the GitHub call), and success
        each produce an audit entry; set Write.url inside the block
        so the success entry links to the created object.

        A write matching an ``elicit`` rule proceeds only after the user
        approves this specific call out-of-band: a single-use URL served
        by this server, behind the same login as the OAuth flow, bound to
        the caller's subject. ``detail`` is the human-readable description
        of the action shown on that approval page.
        """
        assert not action.endswith(":read"), action
        decision = self.policy.check(action, resource)
        if decision.elicit:
            # May raise InputRequired to send the approval URL to the user;
            # nothing is audited then, as the call has not been decided yet.
            decision = await self._approval(ctx, tool, resource, detail, decision)
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
            detail=_APPROVED if decision.reason == _APPROVED else None,
        )

    async def _approval(
        self,
        ctx: MCPRequest,
        tool: str,
        resource: str,
        detail: str | None,
        decision: Decision,
    ) -> Decision:
        """Resolve an ``elicit`` decision by asking the user to approve.

        The returned decision is allowed only when the user ticked Approve
        on the (logged-in, subject-bound, single-use) approval page. A
        client without URL-elicitation support is rejected by the library
        with the spec's MISSING_CLIENT_CAPABILITY error when the request
        below is built.
        """
        subject = None if ctx.auth is None else ctx.auth.subject
        if subject is None:
            # Without this, the library treats the missing subject as a
            # server misconfiguration (opaque 500); degrade to a deny.
            return replace(
                decision,
                reason=f"{decision.reason}, which requires an authenticated user",
            )
        description = f"{detail or tool} in {resource}"
        result = await ctx.elicit(
            "approval",
            SecureFormElicitation(
                {"approve": BooleanSchema(title="Approve this action")},
                message=f"An agent connected as {subject} asks to {description}."
                " Submitting with Approve ticked authorizes this single call.",
            ),
        )
        if (
            result.action == "accept"
            and result.content is not None
            and result.content.get("approve") is True
        ):
            return replace(decision, allowed=True, reason=_APPROVED)
        return replace(decision, reason=f"{description} was not approved by the user")


GATE: Final = web.AppKey("GATE", Gate)


def _subject(ctx: MCPRequest) -> str:
    return "anonymous" if ctx.auth is None else (ctx.auth.subject or "anonymous")



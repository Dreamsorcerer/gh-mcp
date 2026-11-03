"""Application assembly."""

import secrets
from collections.abc import AsyncIterator
from typing import Final

import aiohttp_mcp
from aiohttp import web

from . import __version__
from .audit import AUDIT_LOG, EMAIL, AuditLog, email_scheduler
from .config import (
    DEFAULT_API_URL,
    DEFAULT_AUDIT_PATH,
    DEFAULT_MCP_PATH,
    Config,
)
from .github import github_session
from .policy import PolicyEngine
from .tools import ALL_TOOLS, GATE, Gate

USERS: Final = web.AppKey("USERS", dict[str, str])


async def _check_credentials(username: str, password: str, request: web.Request) -> bool:
    expected = request.config_dict[USERS].get(username)
    matched = secrets.compare_digest(password, expected or "*" * 32)
    return matched and expected is not None


def build_app(cfg: Config) -> web.Application:
    gh_cfg = cfg["github"]
    policy = PolicyEngine(cfg.get("rule", []))
    audit_cfg = cfg.get("audit", {})
    audit_path = audit_cfg.get("path", DEFAULT_AUDIT_PATH)
    audit = AuditLog(audit_path)

    auth_cfg = cfg["auth"]
    authorization = aiohttp_mcp.LocalAuthorization(
        _check_credentials,
        resource=auth_cfg["resource"],
        secret=auth_cfg.get("secret"),
        default_scopes=("github",),
    )

    app = web.Application()
    app[USERS] = auth_cfg["users"]
    app[AUDIT_LOG] = audit
    aiohttp_mcp.setup(
        app,
        name="gh-mcp",
        version=__version__,
        title="GitHub access",
        description="You MUST use this server for authenticated access to Github",
        path=cfg.get("server", {}).get("path", DEFAULT_MCP_PATH),
        tools=aiohttp_mcp.ToolRegistry(*ALL_TOOLS),
        authorization=authorization,
    )

    async def github_client(app: web.Application) -> AsyncIterator[None]:
        async with github_session(
            gh_cfg["token"], gh_cfg.get("api_url", DEFAULT_API_URL)
        ) as sess:
            app[GATE] = Gate(sess, policy, audit)
            yield

    app.cleanup_ctx.append(github_client)
    if (email_cfg := cfg.get("email")) is not None:
        app[EMAIL] = email_cfg
        app.cleanup_ctx.append(email_scheduler)
    return app

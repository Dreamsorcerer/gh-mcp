"""Configuration: TOML file loading and validation."""

import tomllib
from pathlib import Path
from typing import Annotated, Final, Literal, NotRequired, TypedDict

from pydantic import Field, TypeAdapter
from pydantic.experimental.pipeline import validate_as

DEFAULT_API_URL: Final = "https://api.github.com"
DEFAULT_SOCK: Final = Path("gh-mcp.sock")
DEFAULT_MCP_PATH: Final = "/mcp"
DEFAULT_AUDIT_PATH: Final = Path("audit.jsonl")
DEFAULT_EMAIL_HOUR: Final = 7


# TODO(PY315): closed=True
class Constraints(TypedDict):
    """Server-enforced transforms and restrictions for write actions."""

    # Force created PRs to draft status.
    draft_only: NotRequired[bool]
    # Prepended to PR/issue titles (idempotent: not doubled).
    title_prefix: NotRequired[str]
    # Prepended to PR/issue/comment bodies (idempotent).
    body_prefix: NotRequired[str]
    # Appended to PR/issue/comment bodies (idempotent).
    body_suffix: NotRequired[str]
    # Path globs for the ``gh_api_get`` raw passthrough (absent = any).
    paths: NotRequired[list[str]]


# TODO(PY315): closed=True
class Rule(TypedDict):
    """One policy rule. Rules are evaluated in file order; first match wins."""

    # Action globs, e.g. ``["*:read"]``, ``["pr:create", "pr:update"]``.
    actions: Annotated[list[str], validate_as(...).len(1)]
    # ``owner/repo`` globs, e.g. ``["aio-libs/*"]``. Absent = any.
    repos: NotRequired[list[str]]
    effect: NotRequired[Literal["allow", "deny"]]
    constraints: NotRequired[Constraints]


# TODO(PY315): closed=True
class GitHubConfig(TypedDict):
    token: str
    api_url: NotRequired[str]


# TODO(PY315): closed=True
class AuthConfig(TypedDict):
    """OAuth configuration (:class:`aiohttp_mcp.LocalAuthorization`)."""

    # Base URL this server is reachable at, e.g. ``https://mcp.example.org``.
    resource: str
    # (username, password)
    users: Annotated[dict[str, str], validate_as(dict[str, str]).len(1)]
    secret: NotRequired[Annotated[bytes, validate_as(str).transform(bytes.fromhex)]]


# TODO(PY315): closed=True
class ServerConfig(TypedDict):
    # Unix socket the server listens on (put a reverse proxy in front).
    sock: NotRequired[Path]
    path: NotRequired[str]


# TODO(PY315): closed=True
class AuditConfig(TypedDict):
    path: NotRequired[Path]


# TODO(PY315): closed=True
class EmailConfig(TypedDict):
    """Daily digest of the audit log, sent via sendmail."""

    to: str
    sender: Annotated[str, Field(alias="from")]
    # Local hour of day (0-23) at which to send.
    hour: NotRequired[Annotated[int, validate_as(int).ge(0).le(23)]]


# TODO(PY315): closed=True
class Config(TypedDict):
    github: GitHubConfig
    auth: AuthConfig
    server: NotRequired[ServerConfig]
    audit: NotRequired[AuditConfig]
    email: NotRequired[EmailConfig]
    # ``[[rule]]`` tables, in evaluation order.
    rule: NotRequired[list[Rule]]


CONFIG_ADAPTER = TypeAdapter(Config)


def load_config(path: Path) -> Config:
    with path.open("rb") as f:
        data = tomllib.load(f)
    return CONFIG_ADAPTER.validate_python(data)

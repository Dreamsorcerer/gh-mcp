"""Audit log and daily email digest."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Final

from aiohttp import web

from .config import DEFAULT_EMAIL_HOUR, EmailConfig

EMAIL: Final = web.AppKey("EMAIL", EmailConfig)

logger = logging.getLogger("gh_mcp")


class AuditLog:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def record(
        self,
        *,
        subject: str,
        tool: str,
        action: str,
        resource: str,
        decision: str,
        url: str | None = None,
        detail: str | None = None,
    ) -> None:
        entry: dict[str, str] = {
            "time": datetime.now(UTC).isoformat(timespec="seconds"),
            "subject": subject,
            "tool": tool,
            "action": action,
            "resource": resource,
            "decision": decision,
        }
        if url is not None:
            entry["url"] = url
        if detail is not None:
            entry["detail"] = detail
        with self._path.open("a") as f:
            f.write(json.dumps(entry) + "\n")


AUDIT_LOG: Final = web.AppKey("AUDIT_LOG", AuditLog)


def rotate(audit_path: Path) -> Path | None:
    """Move the log aside for sending; new actions log to a fresh file.

    A leftover rotation from a failed send is merged in, so nothing is lost.
    Returns the rotated path, or ``None`` when there is nothing to send.
    """
    pending = audit_path.with_name(audit_path.name + ".sending")
    if audit_path.exists():
        if pending.exists():
            with pending.open("a") as f:
                f.write(audit_path.read_text())
            audit_path.unlink()
        else:
            audit_path.rename(pending)
    return pending if pending.exists() and pending.stat().st_size else None


def render_digest(jsonl: str) -> str:
    """Plain-text digest of audit entries, grouped by resource."""
    groups: dict[str, list[dict[str, str]]] = {}
    count = 0
    for line in jsonl.splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        groups.setdefault(entry.get("resource") or "(no repository)", []).append(entry)
        count += 1
    lines = [f"gh-mcp actions in the last day: {count}", ""]
    for resource in sorted(groups):
        lines.append(resource)
        for e in groups[resource]:
            line = f"  {e['time']} {e['subject']} {e['tool']} [{e['decision']}]"
            if "url" in e:
                line += f" {e['url']}"
            if "detail" in e:
                line += f" ({e['detail']})"
            lines.append(line)
        lines.append("")
    return "\n".join(lines)


async def send_digest(cfg: EmailConfig, audit_path: Path) -> None:
    """Rotate the log, email it, and clear it on success."""
    pending = rotate(audit_path)
    if pending is None:
        return
    content = pending.read_text(encoding="utf-8")
    count = sum(1 for line in content.splitlines() if line.strip())

    msg = EmailMessage()
    msg["To"] = cfg["to"]
    msg["From"] = cfg["sender"]
    msg["Subject"] = f"gh-mcp daily action log ({count} actions)"
    msg.set_content(render_digest(content))

    proc = await asyncio.create_subprocess_exec(
        "sendmail", "-t", "-i", stdin=asyncio.subprocess.PIPE
    )
    await proc.communicate(msg.as_bytes())
    if proc.returncode == 0:
        pending.unlink()
    else:
        logger.error("sendmail exited with %s; digest kept for retry", proc.returncode)


def next_run_in(now: datetime, hour: int) -> timedelta:
    """Time until the next local occurrence of ``hour`` o'clock after ``now``."""
    run = now.replace(hour=hour, minute=5, second=27, microsecond=321)
    if run <= now:
        run += timedelta(days=1)
    return run - now


async def email_scheduler(app: web.Application) -> AsyncIterator[None]:
    cfg = app[EMAIL]
    audit_path = app[AUDIT_LOG].path

    async def run() -> None:
        while True:
            now = datetime.now().astimezone()
            delay = next_run_in(now, cfg.get("hour", DEFAULT_EMAIL_HOUR)).total_seconds()
            await asyncio.sleep(delay)
            try:
                await send_digest(cfg, audit_path)
            except Exception:
                logger.exception("sending the daily digest failed")

    task = asyncio.create_task(run())
    yield
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

import json
import os
import stat
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from gh_mcp.audit import AuditLog, next_run_in, render_digest, rotate, send_digest
from gh_mcp.config import EmailConfig

_EMAIL = TypeAdapter(EmailConfig)


def email_cfg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_code: int = 0
) -> EmailConfig:
    """An EmailConfig with a fake ``sendmail`` on PATH capturing stdin."""
    script = tmp_path / "sendmail"
    outbox = tmp_path / "outbox"
    script.write_text(f"#!/bin/sh\ncat >> {outbox}\nexit {exit_code}\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    return _EMAIL.validate_python({"to": "sam@example.org", "from": "gh-mcp@host"})


def test_record_appends_jsonl(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.record(
        subject="sam", tool="create_pr", action="pr:create", resource="o/r",
        decision="allow", url="https://github.com/o/r/pull/1",
    )
    log.record(
        subject="sam", tool="create_pr", action="pr:create", resource="o/r",
        decision="deny", detail="no matching rule",
    )
    lines = [json.loads(line) for line in log.path.read_text().splitlines()]
    assert lines[0]["url"] == "https://github.com/o/r/pull/1"
    assert lines[1]["detail"] == "no matching rule"
    assert "url" not in lines[1]


def test_render_digest_groups_and_links() -> None:
    entries = [
        {"time": "t1", "subject": "sam", "tool": "create_pr", "action": "pr:create",
         "resource": "o/r", "decision": "allow", "url": "https://g/pr/1"},
        {"time": "t2", "subject": "sam", "tool": "add_comment", "action": "issue:comment",
         "resource": "a/b", "decision": "deny", "detail": "no rule"},
    ]
    digest = render_digest("\n".join(json.dumps(e) for e in entries))
    assert "actions in the last day: 2" in digest
    assert "https://g/pr/1" in digest
    assert digest.index("a/b") < digest.index("o/r")  # grouped, sorted
    assert "(no rule)" in digest


async def test_send_digest_clears_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = email_cfg(tmp_path, monkeypatch)
    log = AuditLog(tmp_path / "audit.jsonl")
    log.record(subject="s", tool="t", action="a", resource="o/r", decision="allow",
               url="https://g/1")
    await send_digest(cfg, log.path)
    sent = (tmp_path / "outbox").read_text()
    assert "To: sam@example.org" in sent
    assert "Subject: gh-mcp daily action log (1 actions)" in sent
    assert "https://g/1" in sent
    assert not log.path.exists()
    assert not log.path.with_name("audit.jsonl.sending").exists()


async def test_send_digest_failure_keeps_log_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = email_cfg(tmp_path, monkeypatch, exit_code=1)
    log = AuditLog(tmp_path / "audit.jsonl")
    log.record(subject="s", tool="t", action="a", resource="o/r", decision="allow")
    await send_digest(cfg, log.path)
    pending = log.path.with_name("audit.jsonl.sending")
    assert pending.exists()

    # A later action plus a successful retry sends both entries.
    log.record(subject="s", tool="t2", action="a", resource="o/r", decision="allow")
    cfg_ok = email_cfg(tmp_path, monkeypatch)
    os.unlink(tmp_path / "outbox")
    await send_digest(cfg_ok, log.path)
    sent = (tmp_path / "outbox").read_text()
    assert "(2 actions)" in sent
    assert not pending.exists()


async def test_send_digest_empty_log_skips_email(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = email_cfg(tmp_path, monkeypatch)
    await send_digest(cfg, tmp_path / "audit.jsonl")
    assert not (tmp_path / "outbox").exists()


def test_rotate_merges_leftover(tmp_path: Path) -> None:
    audit = tmp_path / "audit.jsonl"
    pending = tmp_path / "audit.jsonl.sending"
    pending.write_text('{"a": 1}\n')
    audit.write_text('{"a": 2}\n')
    assert rotate(audit) == pending
    assert pending.read_text() == '{"a": 1}\n{"a": 2}\n'
    assert not audit.exists()


def test_next_run_in() -> None:
    now = datetime(2026, 10, 29, 6, 30)  # noqa: DTZ001 - local, naive by design
    mark = timedelta(minutes=5, seconds=27, microseconds=321)
    assert next_run_in(now, 7) == timedelta(minutes=30) + mark
    assert next_run_in(now, 6) == timedelta(hours=23, minutes=30) + mark
    boundary = now.replace(hour=7, minute=5, second=27, microsecond=321)
    assert next_run_in(boundary, 7) == timedelta(days=1)

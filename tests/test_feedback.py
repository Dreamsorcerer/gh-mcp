from pathlib import Path

from conftest import MakeApp, call_tool
from test_tools import audit_entries


async def test_report_missing_access_logs(make_app: MakeApp, audit_path: Path) -> None:
    client = await make_app([])  # no policy rules needed
    result = await call_tool(
        client, "report_missing_access", {"description": "Need to read gists"}
    )
    assert not result.get("isError")
    (entry,) = audit_entries(audit_path)
    assert entry["action"] == "meta:report"
    assert entry["decision"] == "report"
    assert entry["detail"] == "Need to read gists"

import tomllib
from pathlib import Path

import pytest
from conftest import EXAMPLE, TEST_PASSWORD, TEST_SECRET
from pydantic import TypeAdapter

from gh_mcp.config import Config, load_config

_ADAPTER = TypeAdapter(Config)


def test_example_config_is_not_directly_usable() -> None:
    # secret and user1 are deliberately left without values: an insecure
    # example credential must never be copy-pasteable into a deployment.
    with pytest.raises(tomllib.TOMLDecodeError):
        load_config(EXAMPLE)


def test_example_config_loads(example_config_file: Path) -> None:
    cfg = load_config(example_config_file)
    assert cfg["github"]["token"] == "github_pat_..."
    assert cfg["auth"]["secret"] == bytes.fromhex(TEST_SECRET)
    assert cfg["auth"]["users"] == {"user1": TEST_PASSWORD}
    assert cfg["email"]["sender"] == "gh-mcp@foo.example"
    assert cfg["rule"][1]["constraints"]["draft_only"] is True


_BASE = {
    "github": {"token": "T"},
    "auth": {"resource": "https://mcp.example", "users": {"u": "p"}},
}


# TODO(PY315): with closed=True TypedDicts, assert unknown keys are rejected:
# validate_python({... "constraints": {"draft_onyl": True}}) raises ValidationError.

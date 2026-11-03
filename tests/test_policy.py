import pytest

from gh_mcp.config import Constraints, Rule
from gh_mcp.policy import PolicyEngine, apply_prefix, apply_suffix


def test_default_deny() -> None:
    engine = PolicyEngine([])
    decision = engine.check("pr:create", "aio-libs/aiohttp")
    assert not decision.allowed
    assert "no matching rule" in decision.reason
    assert decision.rule == 0


def test_first_match_wins() -> None:
    engine = PolicyEngine(
        [
            Rule(actions=["pr:create"], repos=["aio-libs/aiohttp"], effect="deny"),
            Rule(actions=["pr:*"], repos=["aio-libs/*"]),
        ]
    )
    assert not engine.check("pr:create", "aio-libs/aiohttp").allowed
    assert engine.check("pr:create", "aio-libs/yarl").allowed
    assert engine.check("pr:update", "aio-libs/aiohttp").allowed


def test_action_and_repo_globs() -> None:
    engine = PolicyEngine([Rule(actions=["*:read"], repos=["aio-libs/*"])])
    assert engine.check("code:read", "aio-libs/aiohttp").allowed
    assert not engine.check("code:read", "other/aiohttp").allowed
    assert not engine.check("issue:create", "aio-libs/aiohttp").allowed


def test_constraints_returned_from_matched_rule() -> None:
    constraints = Constraints(draft_only=True, title_prefix="[agent] ")
    engine = PolicyEngine(
        [Rule(actions=["pr:create"], repos=["o/r"], constraints=constraints)]
    )
    assert engine.check("pr:create", "o/r").constraints is constraints


def test_paths_glob_narrows_raw_rules() -> None:
    engine = PolicyEngine(
        [
            Rule(
                actions=["raw:read"],
                constraints=Constraints(paths=["/repos/o/r/*", "/rate_limit"]),
            )
        ]
    )
    assert engine.check("raw:read", "o/r", path="/repos/o/r/branches").allowed
    assert engine.check("raw:read", "", path="/rate_limit").allowed
    assert not engine.check("raw:read", "", path="/user").allowed
    # Non-raw calls ignore the paths constraint.
    engine2 = PolicyEngine(
        [Rule(actions=["*:read"], constraints=Constraints(paths=["/never"]))]
    )
    assert engine2.check("code:read", "o/r").allowed


@pytest.mark.parametrize(
    ("text", "prefix", "expected"),
    [
        ("Fix bug", "[agent] ", "[agent] Fix bug"),
        ("[agent] Fix bug", "[agent] ", "[agent] Fix bug"),
        ("Fix bug", "", "Fix bug"),
    ],
)
def test_apply_prefix_idempotent(text: str, prefix: str, expected: str) -> None:
    assert apply_prefix(text, prefix) == expected


def test_apply_suffix_idempotent() -> None:
    assert apply_suffix("body", "\n--sig") == "body\n--sig"
    assert apply_suffix("body\n--sig", "\n--sig") == "body\n--sig"


def test_resource_matching_is_case_insensitive() -> None:
    # GitHub owner/repo names are case-insensitive; a differently-cased name
    # must not skip a deny (or constrained) rule and hit a broader one.
    engine = PolicyEngine(
        [
            Rule(actions=["pr:create"], repos=["evil/*"], effect="deny"),
            Rule(actions=["pr:create"], repos=["*"]),
        ]
    )
    assert not engine.check("pr:create", "Evil/Repo").allowed
    assert engine.check("pr:create", "good/repo").allowed

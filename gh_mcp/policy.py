"""Policy engine: maps (action, resource) to a decision with constraints.

Rules are evaluated in configuration order; the first rule whose action and
repo (and, for the raw passthrough, path) globs all match decides the call:
``allow``, ``deny``, or ``elicit`` (allowed only after per-call user
approval). No matching rule means deny.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase

from .config import Constraints, Rule


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    reason: str
    constraints: Constraints
    # 1-based index of the matched rule, 0 when no rule matched.
    rule: int
    # The matched rule wants explicit user approval; ``allowed`` stays False
    # until the approval is obtained (write tools only).
    elicit: bool = False


_NO_CONSTRAINTS = Constraints()


class PolicyEngine:
    def __init__(self, rules: Sequence[Rule]) -> None:
        self._rules = rules

    def check(self, action: str, resource: str, *, path: str | None = None) -> Decision:
        """Decide ``action`` on ``resource`` (``owner/repo``, or ``""``).

        ``path`` is the API path of a raw passthrough call, matched against a
        rule's ``constraints.paths`` globs. GitHub treats owner/repo names
        case-insensitively, so resource and path matching is case-folded to
        stop a differently-cased name falling through to a later rule.
        """
        resource = resource.casefold()
        if path is not None:
            path = path.casefold()
        for index, rule in enumerate(self._rules, start=1):
            if not any(fnmatchcase(action, a) for a in rule["actions"]):
                continue
            if not any(fnmatchcase(resource, r.casefold()) for r in rule.get("repos", ("*",))):
                continue
            constraints = rule.get("constraints", _NO_CONSTRAINTS)
            if (
                path is not None
                and (globs := constraints.get("paths")) is not None
                and not any(fnmatchcase(path, g.casefold()) for g in globs)
            ):
                continue
            effect = rule.get("effect", "allow")
            if effect == "deny":
                return Decision(
                    False,
                    f"{action} on {resource or 'this resource'} is denied by policy",
                    _NO_CONSTRAINTS,
                    index,
                )
            if effect == "elicit":
                return Decision(
                    False,
                    f"{action} on {resource or 'this resource'}"
                    " requires explicit user approval",
                    constraints,
                    index,
                    elicit=True,
                )
            return Decision(True, "allowed", constraints, index)
        return Decision(
            False,
            f"{action} not permitted for {resource or 'this resource'} (no matching rule)",
            _NO_CONSTRAINTS,
            0,
        )


def apply_prefix(text: str, prefix: str) -> str:
    """Prepend ``prefix`` unless already present (idempotent on updates)."""
    if prefix and not text.startswith(prefix):
        return prefix + text
    return text


def apply_suffix(text: str, suffix: str) -> str:
    if suffix and not text.endswith(suffix):
        return text + suffix
    return text

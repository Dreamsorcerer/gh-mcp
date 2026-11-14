gh-mcp
======

An MCP server granting agents fine-grained, policy-gated access to GitHub,
built on `aiohttp-mcp-server <https://github.com/aio-libs/aiohttp-mcp-server>`_.

The server holds the GitHub API token, so agents never see it. Every tool call passes
through a policy engine mapping *(action, resource)* to allow/deny plus server-enforced
constraints the agent cannot override (e.g. PRs forced to draft with a title prefix and
a body preamble).

A daily summary of tool calls that were made is emailed so it is easy to review and
cleanup anything an agent has done.

Note: This is not designed to be a fully flexible package; I have no intention of
publishing it on PyPI. Instead it represents the tools I'm looking for, but it should
be easy to fork and add/edit the tools on offer for your particular needs.

Usage
-----

.. code-block:: bash

    gh-mcp --config gh-mcp.toml

See ``example-config.toml`` for the full configuration format.

Policy
------

Rules are evaluated in file order; the first rule whose ``actions`` and
``repos`` globs match decides the call, and no match means deny:

.. code-block:: toml

    [[rule]]
    actions = ["pr:create", "pr:update"]
    repos = ["aio-libs/*"]
    [rule.constraints]
    draft_only = true
    title_prefix = "[agent] "
    body_prefix = "This PR was agent-authored.\n\n"

Writes are curated tools only (``create_issue``, ``add_comment``,
``create_pr``, ``update_pr``, ``create_fork``, ``create_advisory_fork``);
reads are covered by dedicated tools plus a policy-gated raw GET
passthrough (``gh_api_get``).

Per-call user approval
----------------------

A rule with ``effect = "elicit"`` lets a write tool work beyond the
statically-allowed repositories, with each call individually approved by
the user. The server answers the tool call with a URL elicitation: the
user opens a single-use link served by this server, must be logged in
(the same login as the OAuth flow) as the user the agent is connected
as, and ticks Approve on a page describing the exact action. Only then
does the client's retry perform the write, under the rule's constraints;
an explicit ``deny`` rule earlier in the file still wins. For example,
PRs and forks on any repository:

.. code-block:: toml

    [[rule]]
    actions = ["pr:create", "pr:update", "repo:fork"]
    effect = "elicit"
    [rule.constraints]
    draft_only = true
    title_prefix = "[agent] "

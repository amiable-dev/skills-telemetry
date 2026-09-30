"""ADR-014 decision 9: a coding agent's PR is agent work for its harness, not a bot
to discard (#116).

The #62 filter dropped anything that looked like an app. Verified 2026-09-30 by
running the loader's own call, `gh pr list --json author`, on real public PRs:

    Kralizek/MinimalOpenApi#96        {"is_bot": true, "login": "app/copilot-swe-agent"}
    FirelyTeam/firely-net-sdk#3618    {"is_bot": true, "login": "app/claude"}
    acolomba/angrymiao#8              {"is_bot": true, "login": "app/dependabot"}

`gh search prs` spells the same authors `Copilot` and `claude[bot]` (type Bot),
so both spellings are recognised. Anthropic's label is `claude-code-assisted`,
seen on human-authored PRs (opsmill/infrahub-helm#95).
"""
from __future__ import annotations

import pytest

from warehouse.load_delivery import agent_harness, assisted_by, is_bot

COPILOT_AGENT = {"is_bot": True, "login": "app/copilot-swe-agent"}
CLAUDE_APP = {"is_bot": True, "login": "app/claude"}
DEPENDABOT = {"is_bot": True, "login": "app/dependabot"}


def label(*names):
    return [{"name": n} for n in names]


@pytest.mark.parametrize("author,harness", [
    (COPILOT_AGENT, "copilot"),
    ({"login": "Copilot", "type": "Bot", "is_bot": False}, "copilot"),
    ({"login": "copilot-swe-agent[bot]"}, "copilot"),
    (CLAUDE_APP, "claude-code"),
    ({"login": "claude[bot]", "type": "Bot", "is_bot": False}, "claude-code"),
])
def test_coding_agents_are_recognised_in_every_spelling(author, harness):
    assert agent_harness({"author": author}) == harness
    assert not is_bot({"author": author}), "an agent's PR is work, not a bot to discard"


@pytest.mark.parametrize("author", [DEPENDABOT, {"login": "renovate[bot]"}, {"login": "github-actions[bot]"}])
def test_dependency_bots_are_still_discarded(author):
    assert is_bot({"author": author}) and agent_harness({"author": author}) is None


def test_a_human_called_copilot_is_not_the_agent():
    """`Copilot` counts only when the forge says the author is a bot."""
    assert agent_harness({"author": {"login": "Copilot", "type": "User", "is_bot": False}}) is None


def test_a_human_pr_has_no_agent():
    assert agent_harness({"author": {"login": "amiable-dev", "is_bot": False}}) is None
    assert agent_harness({"author": None}) is None


# --- which arm a change request belongs to ---------------------------------------------------

def test_anthropics_label_is_claude_code_involvement():
    assert assisted_by(label("claude-code-assisted")) == "claude-code"


def test_the_agent_author_is_evidence_for_its_harness():
    assert assisted_by([], COPILOT_AGENT) == "copilot"
    assert assisted_by([], CLAUDE_APP) == "claude-code"


def test_no_evidence_is_unknown_never_none():
    """The label exists only on Team and Enterprise plans with the GitHub app;
    its absence says nothing (ADR-002)."""
    assert assisted_by([], {"login": "amiable-dev"}) == "unknown"


def test_the_explicit_arm_labels_still_work():
    assert assisted_by(label("claude-code")) == "claude-code"
    assert assisted_by(label("copilot")) == "copilot"
    assert assisted_by(label("no-ai")) == "none"


def test_a_control_an_agent_touched_is_not_a_control():
    assert assisted_by(label("no-ai", "claude-code-assisted")) == "claude-code"
    assert assisted_by(label("no-ai"), COPILOT_AGENT) == "copilot"


def test_evidence_for_two_harnesses_is_mixed():
    """The spelling the ticket rollup already uses for the same situation."""
    assert assisted_by(label("copilot", "claude-code-assisted")) == "mixed"
    assert assisted_by(label("claude-code-assisted"), COPILOT_AGENT) == "mixed"


def test_agreeing_evidence_is_not_mixed():
    assert assisted_by(label("claude-code", "claude-code-assisted"), CLAUDE_APP) == "claude-code"


def test_the_change_request_row_reads_the_author():
    from warehouse.load_delivery import to_change_request
    pr = {"number": 96, "headRefName": "copilot/fix-1", "createdAt": "2026-09-01T00:00:00Z",
          "mergedAt": None, "closedAt": None, "labels": [], "reviews": [], "author": COPILOT_AGENT}
    row = to_change_request(pr, "https://github.com/amiable-dev/skills-telemetry")
    assert row["assisted_by"] == "copilot"


def test_logins_are_case_insensitive_as_github_treats_them():
    assert agent_harness({"author": {"login": "Claude[bot]"}}) == "claude-code"

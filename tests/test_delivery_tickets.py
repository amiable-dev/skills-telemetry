"""Issue #62: Dependabot branches minted phantom tickets.

`upload-artifact-7` upper-cased contains `ARTIFACT-7`, and a `\\b`-anchored ticket
regex read it as a ticket key. Every Dependabot PR therefore produced a
`pull_request` row pointing at a ticket nobody created, a `ticket` row for it,
and a member of the "without the skill" cohort that no developer wrote — in a
metric whose whole claim rests on n being truthful.

Two fixes, tested here together because either alone leaves the other's rows: a
ticket key must start the branch or a branch segment, and a bot-authored PR is
not developer work.

Since ADR-013 the ticket is no longer the join key, only enrichment, found by
this regex when the forge links no issue. The phantoms stay tested here because
a phantom ticket still mis-groups work, even if it can no longer mis-join it.
"""
import pytest

from warehouse.change_requests import key_in_branch, ticket_rows
from warehouse.load_delivery import is_bot, ticket_link_rows, to_change_request


def ticket_from_branch(branch: str) -> str:
    """The branch half of the fallback, spelled the way these tests always read."""
    return key_in_branch(branch) or "unattributed"


# --- the phantoms, exactly as they appeared ---

#: Branches the anchor fixes: the phantom key sat mid-segment, after a hyphen.
PHANTOMS = [
    "dependabot/github_actions/actions/upload-artifact-7",
    "dependabot/github_actions/actions/dependency-review-action-5",
    "dependabot/github_actions/astral-sh/setup-uv-10",
    "dependabot/github_actions/docker/build-push-action-6",
]

#: Branches the anchor did NOT fix, because the phantom key *does* start a
#: segment. ADR-013 closed them twice over: a key followed by `.` is a version,
#: and dependabot/ and renovate/ branches carry no ticket by construction. The
#: bot filter still removes the PRs themselves.
PHANTOMS_ONLY_THE_BOT_FILTER_CATCHES = [
    "dependabot/pip/requests-2.32.5",
    "dependabot/npm_and_yarn/types/node-24",
    "renovate/lodash-4.x",
]


@pytest.mark.parametrize("branch", PHANTOMS)
def test_a_dependabot_branch_mints_no_ticket(branch):
    assert ticket_from_branch(branch) == "unattributed", branch


@pytest.mark.parametrize("branch", PHANTOMS_ONLY_THE_BOT_FILTER_CATCHES)
def test_the_phantoms_the_anchor_missed_are_now_closed(branch):
    """These minted ANALYZE-4, INIT-4 and REQUESTS-2 until ADR-013."""
    assert ticket_from_branch(branch) == "unattributed", branch
    assert is_bot({"author": {"login": "dependabot[bot]"}})


def test_the_two_reported_phantoms_are_gone():
    """Named rather than parametrised: these are the rows that were in the
    warehouse, and a regression test should say which."""
    assert ticket_from_branch(
        "dependabot/github_actions/actions/upload-artifact-7") != "ARTIFACT-7"
    assert ticket_from_branch(
        "dependabot/github_actions/actions/dependency-review-action-5") != "ACTION-5"


# --- and the real ones still parse ---

@pytest.mark.parametrize("branch,expected", [
    ("PLAT-42-audit-log", "PLAT-42"),
    ("feature/PLAT-42-audit-log", "PLAT-42"),
    ("chris/STDTEL-63-adr-009-capture", "STDTEL-63"),
    ("STDTEL-63-adr-009-capture", "STDTEL-63"),
    ("feat/ABC1-9", "ABC1-9"),
    ("fix/plat-42-lowercase-branch", "PLAT-42"),
    ("release/v2/OPS-1234-rollback", "OPS-1234"),
])
def test_a_real_ticket_branch_still_parses(branch, expected):
    assert ticket_from_branch(branch) == expected


@pytest.mark.parametrize("branch", ["fix-typo", "", "main", "chore/bump-deps"])
def test_a_branch_with_no_ticket_is_unattributed_not_dropped(branch):
    assert ticket_from_branch(branch) == "unattributed"


def test_a_phantom_ticket_produces_no_ticket_row():
    """The row is what the scorecard counts, so this is the assertion that
    matters: unattributed PRs are kept for cost analysis and excluded from
    outcome analysis, which is exactly what a Dependabot PR deserves."""
    raw = {"number": 7, "headRefName": PHANTOMS[0], "title": "bump", "body": "",
           "createdAt": "2026-09-01T09:00:00Z", "mergedAt": "2026-09-02T09:00:00Z"}
    prs = [to_change_request(raw, "o/r")]
    links = ticket_link_rows(raw, "o/r")
    assert links == []
    assert ticket_rows(prs, links, "payments") == []


# --- bot authorship, in all three spellings gh has used ---

@pytest.mark.parametrize("author", [
    {"login": "dependabot[bot]"},
    {"login": "github-actions[bot]"},
    {"login": "app/dependabot"},
    {"login": "dependabot", "is_bot": True},
])
def test_a_bot_authored_pr_is_recognised(author):
    assert is_bot({"number": 1, "author": author})


@pytest.mark.parametrize("author", [
    {"login": "amiable-dev"},
    {"login": "amiable-dev", "is_bot": False},
    {},
    None,
])
def test_a_human_pr_is_not_mistaken_for_a_bot(author):
    """A missing author is not evidence of a bot. Dropping a PR because a field
    was absent would silently shrink the denominator (ADR-005)."""
    assert not is_bot({"number": 1, "author": author})


def test_the_bot_filter_and_the_regex_cover_different_rows():
    """Neither fix subsumes the other.

    A human branch can still contain a phantom (`chris/upload-artifact-7`), and a
    bot can open a PR from a branch with no phantom at all. Fixing one and
    calling it done leaves the other's rows in the warehouse.
    """
    assert ticket_from_branch("chris/upload-artifact-7") == "unattributed"
    assert not is_bot({"author": {"login": "chris"}})
    assert is_bot({"author": {"login": "dependabot[bot]"}})
    assert ticket_from_branch("dependabot/bump-thing") == "unattributed"


# --- the capture half no longer parses tickets at all (ADR-013) ---
# Its join key is a branch hash, held equal to this side's by
# tests/test_change_requests.py; there is no second regex left to drift.

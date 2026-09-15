"""Issue #62: Dependabot branches minted phantom tickets.

`upload-artifact-7` upper-cased contains `ARTIFACT-7`, and a `\\b`-anchored ticket
regex read it as a ticket key. Every Dependabot PR therefore produced a
`pull_request` row pointing at a ticket nobody created, a `ticket` row for it,
and a member of the "without the skill" cohort that no developer wrote — in a
metric whose whole claim rests on n being truthful.

Two fixes, tested here together because either alone leaves the other's rows: a
ticket key must start the branch or a branch segment, and a bot-authored PR is
not developer work.
"""
import pytest

from warehouse.load_delivery import is_bot, parse_pr, ticket_from_branch, ticket_rows


# --- the phantoms, exactly as they appeared ---

#: Branches the anchor fixes: the phantom key sat mid-segment, after a hyphen.
PHANTOMS = [
    "dependabot/github_actions/actions/upload-artifact-7",
    "dependabot/github_actions/actions/dependency-review-action-5",
    "dependabot/github_actions/astral-sh/setup-uv-10",
    "dependabot/github_actions/docker/build-push-action-6",
]

#: Branches the anchor does NOT fix, because the phantom key *does* start a
#: segment. `requests-2.32.5` is indistinguishable from a ticket key by shape
#: alone, and a regex that excluded it would exclude real keys too. These are
#: removed by the bot filter, which is why both halves of the fix are needed.
PHANTOMS_ONLY_THE_BOT_FILTER_CATCHES = [
    "dependabot/pip/requests-2.32.5",
    "dependabot/npm_and_yarn/types/node-24",
    "renovate/lodash-4.x",
]


@pytest.mark.parametrize("branch", PHANTOMS)
def test_a_dependabot_branch_mints_no_ticket(branch):
    assert ticket_from_branch(branch) == "unattributed", branch


@pytest.mark.parametrize("branch", PHANTOMS_ONLY_THE_BOT_FILTER_CATCHES)
def test_the_anchor_alone_does_not_catch_every_phantom(branch):
    """Written down rather than wished away.

    A branch segment that begins with a package name and ends in a version is
    the same shape as a ticket key, and no regex can tell them apart. The bot
    filter is what removes these rows; if it is ever relaxed, this test says
    what comes back.
    """
    assert ticket_from_branch(branch) != "unattributed", branch
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
    prs = [parse_pr({"number": 7, "headRefName": PHANTOMS[0],
                     "createdAt": "2026-09-01T09:00:00Z",
                     "mergedAt": "2026-09-02T09:00:00Z"}, "o/r")]
    assert prs[0]["ticket_id"] == "unattributed"
    assert ticket_rows(prs, "payments") == []


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


# --- the two halves of the ADR-002 join must agree ---

def test_the_capture_and_delivery_parsers_cannot_drift():
    """`warehouse/` is standalone and imports nothing from `stdtel`, so the same
    expression exists twice on purpose. ADR-002 joins a span's `std.ticket.id`
    to a PR's `ticket_id` on equality, so a difference between them fails
    nothing and silently stops rows matching — which is how #62's phantoms were
    on spans as well as in the `ticket` table.
    """
    from stdtel.enrich import ticket_from_branch as capture_side

    corpus = (PHANTOMS + PHANTOMS_ONLY_THE_BOT_FILTER_CATCHES +
              ["PLAT-42-audit-log", "feature/PLAT-42-audit-log", "chris/STDTEL-63-adr-009-capture",
               "fix/plat-42-lowercase-branch", "release/v2/OPS-1234-rollback",
               "fix-typo", "", "main", "chore/bump-deps"])
    disagreements = [b for b in corpus if capture_side(b) != ticket_from_branch(b)]
    assert not disagreements, f"the join key differs between capture and delivery for: {disagreements}"


@pytest.mark.parametrize("branch", PHANTOMS)
def test_a_dependabot_branch_stamps_no_ticket_on_a_span_either(branch):
    """The fix had to reach capture too: the delivery half stopped minting
    phantom `ticket` rows while spans kept carrying `ARTIFACT-7`."""
    from stdtel.enrich import ticket_from_branch as capture_side

    assert capture_side(branch) == "unattributed", branch

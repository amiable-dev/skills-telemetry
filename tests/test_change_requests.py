"""ADR-013, the delivery half: a forge-neutral change-request record, filled by a
GitHub adapter, joined to capture by branch identity and commit patch-ids.
"""
from __future__ import annotations

import subprocess

import pytest

from stdtel import enrich
from warehouse import change_requests as cr
from warehouse import load_delivery as gh


# --- the two halves of the join must compute the same key ---------------------------

REMOTES = [
    "git@github.com:amiable-dev/skills-telemetry.git",
    "https://github.com/amiable-dev/skills-telemetry",
    "ssh://git@github.com/Amiable-Dev/Skills-Telemetry.git",
    "https://someone@gitlab.example.com:8443/group/sub/proj.git",
    "git@gitlab.com:group/proj.git",
]


@pytest.mark.parametrize("remote", REMOTES)
def test_capture_and_delivery_agree_on_the_repository(remote):
    """`warehouse/` imports nothing from `stdtel`, so the normalisation exists
    twice on purpose. A difference fails nothing: rows simply stop matching."""
    assert cr.repo_id(remote) == enrich.repo_id(remote)


@pytest.mark.parametrize("branch", ["main", "fix/media-hardening-233", "release-0.6.0", "a/b/c"])
def test_capture_and_delivery_agree_on_the_branch_hash(branch):
    rid = "github.com/amiable-dev/skills-telemetry"
    assert cr.branch_hash(rid, branch) == enrich.branch_hash(rid, branch)


def test_a_change_request_id_names_its_forge_and_repository():
    assert cr.cr_id("github", "github.com/o/r", 87) == "github:github.com/o/r!87"


# --- the ticket, as enrichment ---------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("PLAT-42-audit-log", "PLAT-42"),
    ("feature/PLAT-42-audit-log", "PLAT-42"),
    ("STDTEL-102: ADR-013, join work", "STDTEL-102"),
    ("fix/plat-42-lowercase-branch", "PLAT-42"),
    ("release/v2/OPS-1234-rollback", "OPS-1234"),
])
def test_a_real_key_is_found(text, expected):
    assert cr.key_in(text) == expected


@pytest.mark.parametrize("branch", [
    # every phantom this repository has produced, and the shapes behind them
    "dependabot/github_actions/github/codeql-action/analyze-4.38.2",   # ANALYZE-4
    "dependabot/github_actions/github/codeql-action/init-4.38.2",      # INIT-4
    "dependabot/pip/requests-2.31",                                    # REQUESTS-2
    "dependabot/npm_and_yarn/types/node-24",
    "renovate/lodash-4.x",
    "release-0.5.0", "release-0.6.0",                                  # RELEASE-0
    "dependabot/github_actions/actions/upload-artifact-7",             # #62
])
def test_no_phantom_ticket_from_a_bot_or_release_branch(branch):
    assert cr.key_in_branch(branch) is None, branch


def test_a_key_followed_by_a_dot_is_a_version_not_a_ticket():
    assert cr.key_in("chris/requests-2.31") is None
    assert cr.key_in("chris/PLAT-42.fix") is None
    assert cr.key_in("chris/PLAT-42-fix") == "PLAT-42"


def test_forge_links_win_and_the_fallback_says_where_it_looked():
    rid = "github.com/o/r"
    links = cr.ticket_links("github", rid, closes=[86], title="STDTEL-5: x", body="", branch="b")
    assert links == [("github:github.com/o/r#86", "forge")], "the fallback runs only when the forge says nothing"
    assert cr.ticket_links("github", rid, closes=[], title="STDTEL-5: x", body="", branch="PLAT-9-y") == \
        [("STDTEL-5", "title")]
    assert cr.ticket_links("github", rid, closes=[], title="x", body="fixes PLAT-9", branch="z") == \
        [("PLAT-9", "body")]
    assert cr.ticket_links("github", rid, closes=[], title="x", body="", branch="fix/PLAT-9-y") == \
        [("PLAT-9", "branch")]
    assert cr.ticket_links("github", rid, closes=[], title="x", body="", branch="fix/media-hardening-233") == [], \
        "the trailing-number convention of a real repository names no key"


def test_a_lowercase_word_and_number_reads_as_a_key_and_the_forge_outranks_it():
    """Written down rather than wished away. Linear's branches are lowercase
    (`chris/eng-123-title`), so the fallback must accept lowercase keys, and
    then `media-233` has exactly a key's shape. It is only a fallback: the
    forge's own link wins, and a wrong ticket mis-groups tickets without
    touching the join, which is on the branch."""
    rid = "github.com/o/r"
    assert cr.ticket_links("github", rid, closes=[], title="x", body="", branch="fix/media-233") == \
        [("MEDIA-233", "branch")]
    assert cr.ticket_links("github", rid, closes=[233], title="x", body="", branch="fix/media-233") == \
        [("github:github.com/o/r#233", "forge")]


# --- the GitHub adapter ------------------------------------------------------------------

def pr(**over):
    base = {"number": 87, "headRefName": "fix/media-hardening-233", "state": "MERGED",
            "createdAt": "2026-09-01T09:00:00Z", "mergedAt": "2026-09-03T09:00:00Z",
            "closedAt": "2026-09-03T09:00:00Z", "isCrossRepository": False,
            "headRepository": {"name": "skills-telemetry"},
            "headRepositoryOwner": {"login": "amiable-dev"},
            "title": "fix media hardening", "body": "Closes #86",
            "closingIssuesReferences": [{"number": 86}],
            "labels": [{"name": "claude-code"}],
            "reviews": [{"state": "CHANGES_REQUESTED", "submittedAt": "2026-09-01T12:00:00Z"},
                        {"state": "APPROVED", "submittedAt": "2026-09-02T09:00:00Z"}]}
    base.update(over)
    return base


BASE = "amiable-dev/skills-telemetry"


def test_a_pr_becomes_a_change_request_joinable_by_its_branch():
    row = gh.to_change_request(pr(), BASE)
    rid = "github.com/amiable-dev/skills-telemetry"
    assert row["cr_id"] == f"github:{rid}!87" and row["forge"] == "github"
    assert row["source_branch"] == "fix/media-hardening-233"
    assert row["branch_hash"] == enrich.branch_hash(rid, "fix/media-hardening-233"), \
        "the capture side stamps exactly this for work on that branch"
    assert row["state"] == "merged" and row["review_rounds"] == 1


@pytest.mark.parametrize("state,merged,closed,expected", [
    ("MERGED", "2026-09-03T09:00:00Z", "2026-09-03T09:00:00Z", "merged"),
    ("CLOSED", None, "2026-09-03T09:00:00Z", "closed"),
    ("OPEN", None, None, "open"),
])
def test_closed_and_open_prs_are_loaded_and_marked(state, merged, closed, expected):
    """ADR-013 decision 3: abandoned work has a cost, and it was invisible when
    only merged PRs were fetched."""
    row = gh.to_change_request(pr(state=state, mergedAt=merged, closedAt=closed), BASE)
    assert row["state"] == expected


def test_a_fork_hashes_against_the_repository_its_branch_lives_in():
    """The contributor's hook sees the fork's remote, so the delivery side must
    hash the fork too, or no fork PR ever joins."""
    row = gh.to_change_request(pr(isCrossRepository=True, headRepositoryOwner={"login": "someone"}), BASE)
    assert row["branch_hash"] == enrich.branch_hash("github.com/someone/skills-telemetry",
                                                    "fix/media-hardening-233")
    assert row["repo_id"] == "github.com/amiable-dev/skills-telemetry", "the change request belongs to the base"


def test_tickets_come_from_the_forge_first():
    links = gh.ticket_link_rows(pr(), BASE)
    assert links == [{"cr_id": "github:github.com/amiable-dev/skills-telemetry!87",
                      "ticket_id": "github:github.com/amiable-dev/skills-telemetry#86", "source": "forge"}]


def test_policy_results_map_ci_pr_ids_onto_change_requests(tmp_path):
    """CI writes `owner/repo#number`, GitHub-shaped; the adapter owns that."""
    f = tmp_path / "p.jsonl"
    f.write_text('{"pr_id": "amiable-dev/skills-telemetry#87", "policy_id": "a.b", "run_seq": 1, '
                 '"passed": true, "evaluated_at": "2026-09-01T10:00:00Z"}\n')
    rows = gh.policy_rows(f)
    assert rows[0]["cr_id"] == "github:github.com/amiable-dev/skills-telemetry!87"
    assert "pr_id" not in rows[0]


def _patch_for(tmp_path) -> tuple[str, str]:
    """A real commit's patch and its patch-id, computed by real git."""
    r = tmp_path / "r"
    r.mkdir()
    run = lambda *a: subprocess.run(["git", *a], cwd=r, check=True, capture_output=True, text=True).stdout
    run("init", "-q")
    run("config", "user.email", "x@example.com")
    run("config", "user.name", "x")
    (r / "f").write_text("one\n")
    run("add", "f")
    run("commit", "-qm", "c")
    patch = run("format-patch", "-1", "--stdout", "HEAD")
    pid = subprocess.run(["git", "patch-id", "--stable"], input=patch, capture_output=True,
                         text=True).stdout.split()[0]
    return patch, pid


def test_commit_rows_carry_patch_ids_computed_from_the_forges_patch(tmp_path):
    patch, pid = _patch_for(tmp_path)
    rows = gh.commit_rows("github:github.com/o/r!87", ["sha1"], fetch_patch=lambda sha: patch)
    assert rows == [{"cr_id": "github:github.com/o/r!87", "patch_id": pid, "sha": "sha1"}]


def test_a_commit_whose_patch_cannot_be_read_is_skipped_not_invented(tmp_path):
    rows = gh.commit_rows("github:github.com/o/r!87", ["sha1"], fetch_patch=lambda sha: "")
    assert rows == []


# --- ADR-002's rules, carried over the record ------------------------------------------------

def test_a_ticket_whose_change_requests_disagree_on_arm_is_mixed():
    crs = [gh.to_change_request(pr(number=1, labels=[{"name": "claude-code"}]), BASE),
           gh.to_change_request(pr(number=2, labels=[{"name": "copilot"}]), BASE)]
    links = [{"cr_id": c["cr_id"], "ticket_id": "T-1", "source": "title"} for c in crs]
    rows = cr.ticket_rows(crs, links, "payments")
    assert [r["harness_arm"] for r in rows] == ["mixed"]


def test_a_change_request_with_no_ticket_produces_no_ticket_row():
    rows = cr.ticket_rows([gh.to_change_request(pr(), BASE)], [], "payments")
    assert rows == []


def test_every_delivery_column_exists_in_the_schema():
    from tests.test_warehouse import schema_columns
    for table, cols in gh.TABLES.items():
        missing = set(cols) - set(schema_columns(table))
        assert not missing, f"{table} lacks {missing}"

"""A task's PR identity (number and node id): recorded by the CLI flows that
see the PR, kept when the branch is archived, and never replaced by a guess."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from conftest import FakeForge, git

from toolbelt.git.stack import lineage
from toolbelt.git.stack.append import create_stacked_branch
from toolbelt.git.stack.forge import ranked_candidates
from toolbelt.git.stack.store import (
    PrCandidate,
    PrLink,
    RepoIdentity,
    StackStore,
)
from toolbelt.git.stack.sync import sync_stack
from toolbelt.git.workflow import git_branch_clean, record_branch_pr

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def candidate(number: int, created: datetime = T0) -> PrCandidate:
    return PrCandidate(PrLink(number, f"PR_{number}"), created)


def _store(tmp_path: Path) -> tuple[StackStore, int]:
    store = StackStore(tmp_path / "stacks.db")
    repo_id = store.ensure_repo(
        RepoIdentity(tmp_path / ".git", tmp_path, remote_url=None)
    )
    return store, repo_id


# ---- the store -----------------------------------------------------------------


def test_every_task_has_its_own_stable_id_carried_into_the_archive(tmp_path: Path):
    store, repo_id = _store(tmp_path)
    store.set_parent(repo_id, "a", "main")
    store.set_parent(repo_id, "b", "main")
    [a, b] = store.tracked_branch_facts()
    store.set_parent(repo_id, "a", "b")  # reparenting keeps the task's id

    assert a.task_id != b.task_id
    assert store.tracked_branch_facts()[0].task_id == a.task_id

    store.link_pr(repo_id, "a", [candidate(7)])
    store.remove_branch(repo_id, "a", landed=True)

    [archived] = store.removed_branches()
    assert (archived.task_id, archived.pr) == (a.task_id, PrLink(7, "PR_7"))
    # The same name tracked again is a new task.
    store.set_parent(repo_id, "a", "main")
    assert store.tracked_branch_facts()[-1].task_id not in (a.task_id, b.task_id)
    assert store.tracked_branch_facts()[-1].pr is None


def test_a_recorded_link_is_never_replaced(tmp_path: Path):
    store, repo_id = _store(tmp_path)
    store.set_parent(repo_id, "feat", "main")

    first = store.link_pr(repo_id, "feat", [candidate(1)])
    second = store.link_pr(repo_id, "feat", [candidate(2)])

    assert first == second == PrLink(1, "PR_1")
    assert store.get_pr_link(repo_id, "feat") == PrLink(1, "PR_1")


def test_the_first_candidate_in_the_callers_order_is_taken(tmp_path: Path):
    store, repo_id = _store(tmp_path)
    store.set_parent(repo_id, "feat", "main")

    assert store.link_pr(repo_id, "feat", []) is None
    assert store.link_pr(repo_id, "nope", [candidate(1)]) is None  # untracked
    assert store.link_pr(repo_id, "feat", [candidate(5), candidate(4)]) == PrLink(
        5, "PR_5"
    )


def test_a_pr_that_is_another_tasks_is_not_linked_again(tmp_path: Path):
    store, repo_id = _store(tmp_path)
    store.set_parent(repo_id, "feat", "main")
    store.link_pr(repo_id, "feat", [candidate(1)])
    store.remove_branch(repo_id, "feat", landed=True)
    store.set_parent(repo_id, "feat", "main")  # the name is reused

    later = datetime.now(UTC) + timedelta(hours=1)
    linked = store.link_pr(repo_id, "feat", [candidate(1, later), candidate(2, later)])

    assert linked == PrLink(2, "PR_2")


def test_a_pr_older_than_the_previous_tasks_end_is_not_the_new_tasks(
    tmp_path: Path,
):
    store, repo_id = _store(tmp_path)
    store.set_parent(repo_id, "feat", "main")  # never had its PR recorded
    store.remove_branch(repo_id, "feat", landed=True)
    store.set_parent(repo_id, "feat", "main")  # the name is reused
    [archived] = store.removed_branches()
    old = candidate(1, archived.removed_at - timedelta(days=3))
    new = candidate(2, archived.removed_at + timedelta(minutes=5))

    assert store.link_pr(repo_id, "feat", [old]) is None
    assert store.link_pr(repo_id, "feat", [old, new]) == PrLink(2, "PR_2")


# ---- the forge's ranking ---------------------------------------------------------


def test_candidates_are_ranked_open_then_merged_then_closed_newest_first():
    prs = [
        {
            "number": 1,
            "id": "a",
            "state": "CLOSED",
            "createdAt": "2026-01-05T00:00:00Z",
        },
        {
            "number": 2,
            "id": "b",
            "state": "MERGED",
            "createdAt": "2026-01-01T00:00:00Z",
        },
        {
            "number": 3,
            "id": "c",
            "state": "MERGED",
            "createdAt": "2026-01-03T00:00:00Z",
        },
        {"number": 4, "id": "d", "state": "OPEN", "createdAt": "2026-01-02T00:00:00Z"},
    ]

    ranked = ranked_candidates(prs)

    assert [c.link.number for c in ranked] == [4, 3, 2, 1]
    assert ranked[0].created_at == datetime(2026, 1, 2, tzinfo=UTC)


# ---- the CLI flows ---------------------------------------------------------------


def test_sync_records_the_prs_before_cleaning_up_the_landed_branch(
    repo: Path, tmp_path: Path
):
    api_wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=api_wt)
    tests_wt = tmp_path / "wt-tests"
    create_stacked_branch("api_tests", root=api_wt, wt_path=tests_wt)
    forge = FakeForge(
        merged={"devon/api"},
        candidates={
            "devon/api": [candidate(11)],
            "devon/api_tests": [candidate(12)],
        },
    )

    sync_stack(root=tests_wt, forge=forge)

    store = StackStore(Path(lineage.default_db_path()))
    [archived] = store.removed_branches()
    assert (archived.name, archived.landed, archived.pr) == (
        "devon/api",
        True,
        PrLink(11, "PR_11"),
    )
    assert lineage.get_pr_link("devon/api_tests", root=repo) == PrLink(12, "PR_12")


def test_sync_only_asks_about_branches_whose_pr_is_not_recorded_yet(
    repo: Path, tmp_path: Path
):
    api_wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=api_wt)
    forge = FakeForge(candidates={"devon/api": [candidate(11)]})

    sync_stack(root=api_wt, forge=forge)
    sync_stack(root=api_wt, forge=forge)

    assert forge.candidate_lookups == ["devon/api"]


def test_branch_clean_records_the_pr_of_a_branch_it_deletes(repo: Path, tmp_path: Path):
    api_wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=api_wt)
    git("push", "-u", "origin", "devon/api", cwd=api_wt)
    git("push", "origin", "--delete", "devon/api", cwd=api_wt)
    forge = FakeForge(candidates={"devon/api": [candidate(21)]})

    git_branch_clean(root=repo, forge=forge)

    [archived] = StackStore(lineage.default_db_path()).removed_branches()
    assert (archived.name, archived.pr) == ("devon/api", PrLink(21, "PR_21"))


def test_recording_the_pr_of_an_untracked_branch_is_a_no_op(repo: Path):
    forge = FakeForge(candidates={"scratch": [candidate(1)]})

    record_branch_pr("scratch", root=repo, forge=forge)

    assert lineage.get_pr_link("scratch", root=repo) is None


def test_recording_skips_the_lookup_once_a_link_exists(repo: Path, tmp_path: Path):
    api_wt = tmp_path / "wt-api"
    create_stacked_branch("api", root=repo, wt_path=api_wt)
    forge = FakeForge(candidates={"devon/api": [candidate(3)]})

    record_branch_pr("devon/api", root=repo, forge=forge)
    record_branch_pr("devon/api", root=repo, forge=forge)

    assert forge.candidate_lookups == ["devon/api"]
    assert asyncio.run(forge.pr_candidates("devon/api"))[0].link.number == 3

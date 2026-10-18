"""A branch's last good lookup, saved in the stack DB and read back."""

from datetime import UTC, datetime
from pathlib import Path

from toolbelt.git.stack.status import BranchStatus, CiState, PrState, ReviewState
from toolbelt.git.stack.store import PrLookupRow, StackStore
from toolbelt.ui import lookup_cache
from toolbelt.ui.conflict_commit import ConflictCommit
from toolbelt.ui.lookup_cache import LookupCache, SavedLookup
from toolbelt.ui.pr_activity import PrFacts, TimelineItem

WHEN = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
STATUS = BranchStatus(
    pr=PrState.OPEN,
    ci=CiState.FAILED,
    review=ReviewState.REQUESTED,
    url="https://github.com/o/r/pull/5",
    open_comments=2,
    bot_comments=1,
    has_conflicts=True,
    additions=10,
    deletions=3,
    assignees=("devon",),
    reviewers=("sam", "kim"),
)
FACTS = PrFacts(
    number=5,
    title="Add the thing",
    author="devon",
    base_ref="main",
    head_oid="abc",
    state="OPEN",
    is_draft=False,
    created_at=WHEN,
    merged_at=None,
    closed_at=None,
    requested_reviewers=("sam",),
    commit_rollups=(("abc", "FAILURE"), ("def", None)),
    items=(
        TimelineItem(
            time=WHEN,
            kind="commit",
            actor="devon",
            detail="Fix it",
            subject=None,
            oid="abc",
            ci="FAILURE",
            ci_done=WHEN,
        ),
    ),
    additions=10,
    deletions=3,
)
CONFLICT = ConflictCommit(
    sha="abc",
    subject="Fix it",
    files=("a.py", "b.py"),
    source="pr",
    step=None,
    steps=None,
)


def saved(**overrides) -> SavedLookup:
    base = {
        "status": STATUS,
        "facts": FACTS,
        "conflict": CONFLICT,
        "fetched_at": WHEN,
        "full_at": WHEN,
        "etags": {"repos/o/r/pulls/5": '"v1"'},
    }
    base.update(overrides)
    return SavedLookup(**base)  # type: ignore[arg-type]


def cache(tmp_path: Path) -> LookupCache:
    return LookupCache(lambda: tmp_path / "stacks.db")


def test_a_lookup_comes_back_as_it_was_saved(tmp_path):
    c = cache(tmp_path)

    c.save(1, "devon/a", saved())

    assert c.load() == {(1, "devon/a"): saved()}


def test_a_branch_without_a_pr_round_trips_too(tmp_path):
    c = cache(tmp_path)
    nothing = saved(
        status=BranchStatus(PrState.NONE, CiState.NONE, ReviewState.NONE, None),
        facts=None,
        conflict=None,
        etags={},
    )

    c.save(1, "devon/b", nothing)

    assert c.load()[(1, "devon/b")] == nothing


def test_saving_again_replaces_the_row_and_delete_removes_it(tmp_path):
    c = cache(tmp_path)
    c.save(1, "devon/a", saved())
    c.save(1, "devon/a", saved(etags={}))

    assert c.load()[(1, "devon/a")].etags == {}

    c.delete(1, "devon/a")

    assert c.load() == {}


def test_a_row_of_another_schema_is_ignored(tmp_path):
    c = cache(tmp_path)
    c.save(1, "devon/a", saved())
    store = StackStore(tmp_path / "stacks.db")
    (row,) = store.pr_lookups()
    store.save_pr_lookup(
        PrLookupRow(
            repo_id=row.repo_id,
            branch=row.branch,
            fetched_at=row.fetched_at,
            full_at=row.full_at,
            record=row.record,
            schema=lookup_cache.RECORD_SCHEMA + 1,
        )
    )
    store.close()

    assert c.load() == {}

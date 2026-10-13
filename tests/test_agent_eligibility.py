"""When an autonomous agent may pick a task up, and the reason when it may not."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from toolbelt.autonomous_agents.eligibility import (
    TaskFacts,
    World,
    decide,
    global_blockers,
    working_days_between,
)
from toolbelt.autonomous_agents.limits import AgentLimits, load_limits
from toolbelt.autonomous_agents.screen import ScreenState
from toolbelt.git.stack.store import AgentRun, TokenCounts

# A Tuesday at noon, local time.
NOW = datetime(2026, 10, 13, 12, 0).astimezone()
LIMITS = AgentLimits()


def task(**changes) -> TaskFacts:
    base = TaskFacts(
        task_id="t1",
        action_kind="fix_ci",
        pr_open=True,
        head_sha="abc",
        last_commit_at=NOW - timedelta(hours=3),
        has_worktree=True,
        worktree_clean=True,
        worktree_busy=False,
        claimed_by_other=False,
        bot_reviews=0,
        failed_ci_commits=1,
    )
    return replace(base, **changes)


def world(runs=(), **changes) -> World:
    base = World(
        now=NOW,
        enabled=True,
        limits=LIMITS,
        screen=ScreenState(awake=True, locked=False),
        auth_ok=True,
        runs=list(runs),
    )
    return replace(base, **changes)


def run(
    *,
    task_id="t1",
    head="abc",
    started=NOW - timedelta(hours=2),
    ended: datetime | None = None,
    cost=0.01,
    state="succeeded",
) -> AgentRun:
    return AgentRun(
        run_id=f"r-{started.timestamp()}-{task_id}",
        task_id=task_id,
        repo_id=1,
        branch="devon/a",
        trigger="fix_ci",
        head_sha=head,
        model=LIMITS.model,
        state=state,
        started_at=started,
        ended_at=ended
        if ended is not None
        else (None if state == "running" else started + timedelta(minutes=5)),
        session_id=None,
        turns=5,
        cost_usd=cost,
        tokens=TokenCounts(),
        summary=None,
        error=None,
    )


def reason_code(task_facts: TaskFacts, w: World) -> str | None:
    reason = decide(task_facts, w)
    return reason.code if reason else None


# ---- working days ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (date(2026, 10, 13), date(2026, 10, 13), 0),
        (date(2026, 10, 12), date(2026, 10, 13), 1),  # Mon -> Tue
        (date(2026, 10, 12), date(2026, 10, 15), 3),  # Mon -> Thu
        (date(2026, 10, 12), date(2026, 10, 16), 4),  # Mon -> Fri
        (date(2026, 10, 9), date(2026, 10, 12), 1),  # Fri -> Mon: weekend skipped
        (date(2026, 10, 9), date(2026, 10, 14), 3),  # Fri -> Wed
        (date(2026, 10, 10), date(2026, 10, 11), 0),  # Sat -> Sun
    ],
)
def test_working_days_skip_weekends(start, end, expected):
    assert working_days_between(start, end) == expected


# ---- a task that may start -------------------------------------------------------------------


def test_a_healthy_task_with_agents_on_may_start():
    assert decide(task(), world()) is None


# ---- reasons, most task-specific first -----------------------------------------------------------


def test_agents_switched_off_is_the_reason_for_every_task():
    assert reason_code(task(), world(enabled=False)) == "disabled"


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"pr_open": False}, "pr_not_open"),
        ({"last_commit_at": None}, "inactive"),
        ({"claimed_by_other": True}, "claimed"),
        ({"has_worktree": False}, "no_worktree"),
        ({"worktree_busy": True}, "worktree_busy"),
        ({"worktree_clean": False}, "worktree_dirty"),
        ({"bot_reviews": 3}, "thrash"),
        ({"failed_ci_commits": 3}, "thrash"),
    ],
)
def test_task_conditions(changes, code):
    assert reason_code(task(**changes), world()) == code


def test_a_branch_untouched_for_more_than_three_working_days_is_inactive():
    # Tuesday noon: Friday 09:00 is 2 working days back (Mon, Tue), Monday of
    # last week is 6.
    assert decide(task(last_commit_at=NOW - timedelta(days=4)), world()) is None
    old = decide(task(last_commit_at=NOW - timedelta(days=8)), world())
    assert old is not None and old.code == "inactive"
    assert "working days" in old.text


def test_the_runs_already_made_on_this_commit_are_limited():
    runs = [run(started=NOW - timedelta(days=2)), run(started=NOW - timedelta(days=3))]

    assert reason_code(task(), world(runs)) == "runs_per_head"
    assert reason_code(task(head_sha="new"), world(runs)) is None


def test_runs_in_24_hours_are_limited_per_task_and_say_when_it_clears():
    runs = [
        run(head=f"h{i}", started=NOW - timedelta(hours=20 - i, minutes=0))
        for i in range(3)
    ]

    reason = decide(task(head_sha="fresh"), world(runs))

    assert reason is not None and reason.code == "task_runs_per_day"
    assert reason.until == runs[0].started_at + timedelta(hours=24)


def test_one_tasks_spend_in_24_hours_is_limited():
    runs = [run(head="h1", started=NOW - timedelta(hours=3), cost=0.60)]

    assert reason_code(task(head_sha="fresh"), world(runs)) == "task_usd_per_day"


def test_other_tasks_runs_do_not_count_against_this_one():
    runs = [
        run(task_id="other", head="abc", started=NOW - timedelta(hours=1))
        for _ in range(5)
    ]

    assert reason_code(task(), world(runs)) is None


# ---- all agents ---------------------------------------------------------------------------------


def test_the_global_daily_cap_blocks_every_task_and_says_when_it_clears():
    runs = [
        run(task_id=f"x{i}", started=NOW - timedelta(hours=10 - i), cost=1.0)
        for i in range(3)
    ]

    reason = decide(task(), world(runs))

    assert reason is not None and reason.code == "global_usd_per_day"
    assert reason.until == runs[0].started_at + timedelta(hours=24)


def test_the_global_monthly_cap_clears_on_the_first_of_next_month():
    runs = [run(task_id="x", started=NOW - timedelta(days=5), cost=30.0)]

    reason = decide(task(), world(runs))

    assert reason is not None and reason.code == "global_usd_per_month"
    assert reason.until == datetime(2026, 11, 1, tzinfo=UTC)


def test_the_monthly_cap_rolls_over_a_december():
    december = datetime(2026, 12, 15, 12, 0, tzinfo=UTC)
    runs = [run(task_id="x", started=december - timedelta(days=2), cost=31.0)]

    (reason,) = global_blockers(world(runs, now=december))

    assert reason.until == datetime(2027, 1, 1, tzinfo=UTC)


def test_sign_in_and_the_screen_block_every_task():
    assert reason_code(task(), world(auth_ok=False)) == "auth"
    assert (
        reason_code(task(), world(screen=ScreenState(False, False))) == "screen_asleep"
    )
    assert reason_code(task(), world(screen=ScreenState(True, True))) == "screen_locked"


def test_an_unread_screen_or_unchecked_sign_in_does_not_block():
    assert decide(task(), world(screen=None, auth_ok=None)) is None


def test_only_so_many_runs_work_at_once():
    runs = [run(task_id="other", state="running")]

    assert reason_code(task(), world(runs)) == "concurrency"


def test_a_task_waits_out_a_back_off_after_its_last_run():
    ended = NOW - timedelta(minutes=3)
    runs = [run(head="old", started=ended - timedelta(minutes=5), ended=ended)]

    reason = decide(task(head_sha="new"), world(runs))

    assert reason is not None and reason.code == "backoff"
    assert reason.until == ended + timedelta(seconds=LIMITS.backoff_seconds)


def test_the_back_off_is_over_after_it_passes():
    ended = NOW - timedelta(minutes=30)
    runs = [run(head="old", started=ended - timedelta(minutes=5), ended=ended)]

    assert decide(task(head_sha="new"), world(runs)) is None


def test_the_banner_lists_everything_stopping_all_agents():
    codes = [
        b.code
        for b in global_blockers(
            world(enabled=False, auth_ok=False, screen=ScreenState(True, True))
        )
    ]

    assert codes == ["disabled", "auth", "screen_locked"]


# ---- limits ----------------------------------------------------------------------------------------


def test_limits_default_to_conservative_values():
    limits = AgentLimits()

    assert (limits.model, limits.run_usd, limits.run_turns) == (
        "claude-haiku-5-5",
        0.25,
        40,
    )
    assert limits.run_timeout_seconds == 900
    assert (limits.global_usd_per_day, limits.global_usd_per_month) == (3.0, 30.0)


def test_config_overrides_only_what_it_names(tmp_path: Path):
    config = tmp_path / "config.yaml"
    config.write_text("agents:\n  run_usd: 0.5\n  global_usd_per_day: 5\nother: 1\n")

    limits = load_limits(config)

    assert (limits.run_usd, limits.global_usd_per_day, limits.run_turns) == (
        0.5,
        5.0,
        40,
    )


def test_no_config_or_no_agents_section_gives_the_defaults(tmp_path: Path):
    assert load_limits(tmp_path / "missing.yaml") == AgentLimits()
    config = tmp_path / "config.yaml"
    config.write_text("dotfiles:\n  repos: []\n")
    assert load_limits(config) == AgentLimits()


def test_an_unknown_or_nonpositive_limit_is_rejected(tmp_path: Path):
    config = tmp_path / "config.yaml"
    config.write_text("agents:\n  run_usd: 0\n")
    with pytest.raises(ValidationError):
        load_limits(config)
    config.write_text("agents:\n  rn_usd: 1\n")
    with pytest.raises(ValidationError):
        load_limits(config)

"""Token spend: reading model calls out of session transcripts, pricing them,
and attributing them to tasks through claim spans. Transcripts are written to
a temp dir; nothing reads the real ~/.claude."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from toolbelt.git.stack.store import StackStore, TokenCounts, UsageCall
from toolbelt.token_spend import ingest as ingest_module
from toolbelt.token_spend.ingest import ingest_transcripts
from toolbelt.token_spend.pricing import price_usage
from toolbelt.token_spend.transcripts import (
    parse_calls,
    tokens_of,
    transcript_files,
)

T0 = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)

# What Claude Code reported for this exact call (Agent SDK, total_cost_usd).
SDK_REPORTED_USAGE = {
    "input_tokens": 2,
    "output_tokens": 5,
    "cache_read_input_tokens": 0,
    "cache_creation_input_tokens": 14981,
    "cache_creation": {
        "ephemeral_1h_input_tokens": 14981,
        "ephemeral_5m_input_tokens": 0,
    },
}


def usage(
    input_tokens: int = 10,
    output_tokens: int = 20,
    cache_read: int = 100,
    write_1h: int = 0,
    write_5m: int = 0,
) -> dict:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": write_5m + write_1h,
        "cache_creation": {
            "ephemeral_5m_input_tokens": write_5m,
            "ephemeral_1h_input_tokens": write_1h,
        },
    }


def row(
    message_id: str,
    *,
    session: str = "s1",
    model: str = "claude-sonnet-5-5",
    at: datetime = T0,
    message_usage: dict | None = None,
    agent_id: str | None = None,
    kind: str = "assistant",
) -> str:
    document: dict = {
        "type": kind,
        "sessionId": session,
        "timestamp": at.isoformat().replace("+00:00", "Z"),
        "cwd": "/wt/api/a",
        "message": {
            "id": message_id,
            "model": model,
            "usage": message_usage if message_usage is not None else usage(),
        },
    }
    if agent_id:
        document["agentId"] = agent_id
    return json.dumps(document)


def write_transcript(path: Path, *lines: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(line + "\n" for line in lines))


# ---- pricing ------------------------------------------------------------------------------


def test_a_priced_call_matches_what_claude_code_itself_reported():
    assert price_usage("claude-sonnet-5-5", SDK_REPORTED_USAGE) == pytest.approx(
        0.059978, abs=1e-6
    )


def test_a_model_the_price_table_does_not_know_has_no_price():
    assert price_usage("claude-from-the-future-9", usage()) is None


def test_a_partial_row_whose_cache_totals_disagree_still_prices():
    partial = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_creation": {"ephemeral_1h_input_tokens": 2020},
    }

    assert price_usage("claude-sonnet-5-5", partial) == pytest.approx(
        2020 * 4 / 1_000_000
    )


# ---- reading transcripts ----------------------------------------------------------------------


def test_tokens_split_cache_writes_by_lifetime_when_the_response_says_how():
    tokens = tokens_of(usage(write_5m=7, write_1h=9))

    assert tokens == TokenCounts(
        input=10, output=20, cache_read=100, cache_write_5m=7, cache_write_1h=9
    )


def test_an_old_response_without_a_breakdown_counts_its_cache_writes_as_5m():
    old = {"input_tokens": 1, "output_tokens": 2, "cache_creation_input_tokens": 50}

    assert tokens_of(old) == TokenCounts(input=1, output=2, cache_write_5m=50)


def test_only_assistant_rows_with_usage_and_a_real_model_are_calls():
    lines = [
        row("m1"),
        row("m2", kind="user"),
        row("m3", model="<synthetic>"),
        json.dumps({"type": "assistant", "message": {"id": "m4", "model": "x"}}),
        'broken {"usage": ',
        row("m5", agent_id="a1"),
    ]

    calls, unreadable = parse_calls(lines)

    assert [(c.message_id, c.agent_id) for c in calls] == [("m1", None), ("m5", "a1")]
    assert unreadable == 1


def test_transcript_files_include_subagent_transcripts(tmp_path: Path):
    write_transcript(tmp_path / "proj" / "s1.jsonl", row("m1"))
    write_transcript(
        tmp_path / "proj" / "s1" / "subagents" / "agent-a.jsonl", row("m2")
    )
    write_transcript(tmp_path / "proj" / "s1" / "notes" / "other.jsonl", row("m3"))

    names = [p.name for p in transcript_files(tmp_path)]

    assert sorted(names) == ["agent-a.jsonl", "s1.jsonl"]


# ---- ingesting ---------------------------------------------------------------------------------


def test_ingest_reads_each_response_once_and_only_new_lines_next_time(tmp_path: Path):
    store = StackStore(tmp_path / "stacks.db")
    transcript = tmp_path / "projects" / "proj" / "s1.jsonl"
    write_transcript(transcript, row("m1"), row("m1"), row("m2"))

    first = ingest_transcripts(store, tmp_path / "projects")
    again = ingest_transcripts(store, tmp_path / "projects")

    assert (first.files_read, first.new_calls) == (1, 2)
    assert (again.files_read, again.new_calls) == (0, 0)

    with transcript.open("a") as handle:
        handle.write(row("m3") + "\n")
    third = ingest_transcripts(store, tmp_path / "projects")
    assert (third.files_read, third.new_calls) == (1, 1)


def test_a_partial_last_line_waits_until_it_is_complete(tmp_path: Path):
    store = StackStore(tmp_path / "stacks.db")
    transcript = tmp_path / "projects" / "proj" / "s1.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(row("m1") + "\n" + row("m2")[:40])

    first = ingest_transcripts(store, tmp_path / "projects")
    transcript.write_text(row("m1") + "\n" + row("m2") + "\n")
    second = ingest_transcripts(store, tmp_path / "projects")

    assert first.new_calls == 1
    assert second.new_calls == 1


def test_the_complete_row_of_a_response_replaces_its_partial_first_row(tmp_path: Path):
    store = StackStore(tmp_path / "stacks.db")
    projects = tmp_path / "projects"
    transcript = projects / "proj" / "s1.jsonl"
    write_transcript(transcript, row("m1", message_usage=usage(0, 0, 0, write_1h=2020)))
    ingest_transcripts(store, projects)
    with transcript.open("a") as handle:
        handle.write(
            row("m1", message_usage=usage(2, 8475, 965239, write_1h=868)) + "\n"
        )
        handle.write(row("m1", message_usage=usage(0, 1, 0)) + "\n")
    ingest_transcripts(store, projects)

    (model,) = store.usage_by_model(T0 - timedelta(hours=1), T0 + timedelta(hours=1))
    assert model.calls == 1
    assert model.tokens == TokenCounts(
        input=2, output=8475, cache_read=965239, cache_write_1h=868
    )


def test_a_replaced_transcript_is_read_again_from_the_start(tmp_path: Path):
    store = StackStore(tmp_path / "stacks.db")
    projects = tmp_path / "projects"
    transcript = projects / "proj" / "s1.jsonl"
    write_transcript(transcript, row("m1"), row("m2"), row("m3"))
    ingest_transcripts(store, projects)

    write_transcript(transcript, row("m1"))
    result = ingest_transcripts(store, projects)

    assert result.files_read == 1
    assert result.new_calls == 0


def test_a_model_without_a_price_is_reported_and_priced_when_the_table_learns_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store = StackStore(tmp_path / "stacks.db")
    projects = tmp_path / "projects"
    write_transcript(
        projects / "proj" / "s1.jsonl",
        row("m1", model="claude-new-1"),
        row("m2", model="claude-new-1"),
        row("m3"),
    )

    first = ingest_transcripts(store, projects)

    assert first.unpriced_models == {"claude-new-1": 2}
    window = (T0 - timedelta(hours=1), T0 + timedelta(hours=1))
    by_model = {m.model: m for m in store.usage_by_model(*window)}
    assert by_model["claude-new-1"].unpriced_calls == 2
    assert by_model["claude-new-1"].cost_usd == 0
    assert by_model["claude-sonnet-5-5"].unpriced_calls == 0

    real = ingest_module.price_usage
    monkeypatch.setattr(
        ingest_module,
        "price_usage",
        lambda model, u: 0.5 if model == "claude-new-1" else real(model, u),
    )
    second = ingest_transcripts(store, projects)

    assert second.newly_priced == 2
    assert second.unpriced_models == {}
    by_model = {m.model: m for m in store.usage_by_model(*window)}
    assert by_model["claude-new-1"].cost_usd == pytest.approx(1.0)


# ---- attribution through claims ------------------------------------------------------------------


def call(message_id: str, at: datetime, *, session: str = "s1", cost: float = 1.0):
    return UsageCall(
        message_id=message_id,
        session_id=session,
        agent_id=None,
        model="claude-sonnet-5-5",
        at=at,
        cwd=None,
        tokens=TokenCounts(input=10),
        usage={},
        cost_usd=cost,
    )


def claim(
    store: StackStore,
    task_id: str | None,
    first: datetime,
    last: datetime,
    *,
    session="s1",
):
    store.record_claim_seen(
        holder=session,
        session_id=session,
        agent_id=None,
        holder_kind="agent",
        claim_kind="ref",
        target=f"ref-{task_id}",
        task_id=task_id,
        touched_at=first,
        seen_at=first,
        max_gap=timedelta(hours=1),
    )
    store.record_claim_seen(
        holder=session,
        session_id=session,
        agent_id=None,
        holder_kind="agent",
        claim_kind="ref",
        target=f"ref-{task_id}",
        task_id=task_id,
        touched_at=first,
        seen_at=last,
        max_gap=timedelta(hours=1),
    )


def test_spend_goes_to_the_task_a_claim_covered_at_the_time(tmp_path: Path):
    store = StackStore(tmp_path / "stacks.db")
    claim(store, "task-a", T0, T0 + timedelta(minutes=10))
    store.add_usage_calls(
        [
            call("in", T0 + timedelta(minutes=5), cost=2.0),
            call("after", T0 + timedelta(minutes=30), cost=3.0),
            call("other-session", T0 + timedelta(minutes=5), session="s2", cost=4.0),
        ],
        transcript="t",
        offset=1,
    )

    usage_by_task = {
        u.task_id: u.cost_usd
        for u in store.usage_by_task(T0 - timedelta(hours=1), T0 + timedelta(hours=1))
    }

    assert usage_by_task == {"task-a": 2.0, None: 7.0}


def test_a_call_covered_by_two_tasks_claims_is_split_between_them(tmp_path: Path):
    store = StackStore(tmp_path / "stacks.db")
    claim(store, "task-a", T0, T0 + timedelta(minutes=10))
    claim(store, "task-b", T0, T0 + timedelta(minutes=10))
    store.add_usage_calls(
        [call("m1", T0 + timedelta(minutes=5), cost=2.0)], transcript="t", offset=1
    )

    rows = store.usage_by_task(T0 - timedelta(hours=1), T0 + timedelta(hours=1))

    assert {u.task_id: (u.calls, u.cost_usd) for u in rows} == {
        "task-a": (0.5, 1.0),
        "task-b": (0.5, 1.0),
    }


def test_a_claim_that_matched_no_task_does_not_attribute_spend(tmp_path: Path):
    store = StackStore(tmp_path / "stacks.db")
    claim(store, None, T0, T0 + timedelta(minutes=10))
    store.add_usage_calls(
        [call("m1", T0 + timedelta(minutes=5), cost=2.0)], transcript="t", offset=1
    )

    rows = store.usage_by_task(T0 - timedelta(hours=1), T0 + timedelta(hours=1))

    assert [(u.task_id, u.cost_usd) for u in rows] == [(None, 2.0)]


# ---- the summary -----------------------------------------------------------------------------


def test_the_summary_has_windows_and_names_an_unpriced_model(tmp_path: Path):
    from toolbelt.token_spend.report import format_summary
    from toolbelt.token_spend.summary import summarize

    store = StackStore(tmp_path / "stacks.db")
    now = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
    priced = call("m1", now - timedelta(hours=2), cost=2.0)
    unpriced = call("m2", now - timedelta(days=3), cost=None)
    unpriced = UsageCall(**{**unpriced.__dict__, "model": "claude-new-1"})
    old = call("m3", now - timedelta(days=20), cost=5.0)
    store.add_usage_calls([priced, unpriced, old], transcript="t", offset=1)

    summary = summarize(store, now)

    assert [(w.label, w.calls, w.cost_usd) for w in summary.windows] == [
        ("Last 24 hours", 1, 2.0),
        ("Last 7 days", 2, 2.0),
        ("Last 30 days", 3, 7.0),
        ("This month (UTC)", 2, 2.0),
    ]
    assert summary.unpriced_models == {"claude-new-1": 1}
    text = format_summary(summary)
    assert "No price for: claude-new-1 (1 calls)" in text
    assert "(no claim covered it)" in text

"""Decisions: what an agent may propose, what the user may answer, and the book
that stores them in the stack database."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from toolbelt.decisions.book import AlreadyAnswered, DecisionBook, NoSuchDecision
from toolbelt.decisions.model import (
    InvalidDecision,
    PointAnswer,
    answers_from_data,
    describe_answers,
    points_from_data,
    points_from_json,
    points_to_json,
)
from toolbelt.task_claims.resolution import TrackedTask

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
TASK = TrackedTask(
    repo_id=1,
    repo_name="api",
    branch="devon/a",
    task_id="task-a",
    task_ref="RD-1",
    worktree=Path("/wt/a"),
)

RAW_POINTS = [
    {
        "id": "D1",
        "title": "Where does it live?",
        "context": "The store or a new module.",
        "options": [
            {
                "id": "O1",
                "title": "In the store",
                "summary": "One table.",
                "context": "More.",
                "benefits": "Simple",
                "costs": "A migration",
                "risks": "None",
            },
            {"id": "O2", "title": "In a module"},
        ],
        "recommendation": {"option_id": "O1", "reasoning": "It is simpler."},
    },
    {"id": "D2", "title": "Anything else?"},
]


class TestProposing:
    def test_points_are_read_with_their_options_and_recommendation(self):
        points = points_from_data(RAW_POINTS)

        assert [p.id for p in points] == ["D1", "D2"]
        assert points[0].options[0].benefits == "Simple"
        assert points[0].recommendation is not None
        assert points[0].recommendation.option_id == "O1"
        assert points[1].options == ()  # a point may be answered in words alone

    def test_points_survive_the_database_round_trip(self):
        points = points_from_data(RAW_POINTS)

        assert points_from_json(points_to_json(points)) == points

    @pytest.mark.parametrize(
        ("raw", "message"),
        [
            ([], "non-empty"),
            ("x", "non-empty"),
            ([{"title": "t"}], "id is required"),
            ([{"id": "D1"}], "title is required"),
            ([{"id": "D1", "title": "t"}, {"id": "D1", "title": "u"}], "unique"),
            (
                [
                    {
                        "id": "D1",
                        "title": "t",
                        "options": [
                            {"id": "O", "title": "a"},
                            {"id": "O", "title": "b"},
                        ],
                    }
                ],
                "option ids must be unique",
            ),
            (
                [
                    {
                        "id": "D1",
                        "title": "t",
                        "options": [{"id": "O", "title": "a"}],
                        "recommendation": {"option_id": "Z", "reasoning": "r"},
                    }
                ],
                "not there",
            ),
            ([{"id": "D1", "title": 5}], "must be text"),
        ],
    )
    def test_what_does_not_fit_is_refused_with_why(self, raw, message):
        with pytest.raises(InvalidDecision, match=message):
            points_from_data(raw)


class TestAnswering:
    points = points_from_data(RAW_POINTS)

    def test_each_point_is_answered_by_an_option_or_words_or_both(self):
        answers = answers_from_data(
            [
                {"point_id": "D1", "option_id": "O2", "text": " but check it "},
                {"point_id": "D2", "text": "No."},
            ],
            self.points,
        )

        assert answers == (
            PointAnswer("D1", "O2", "but check it"),
            PointAnswer("D2", None, "No."),
        )

    @pytest.mark.parametrize(
        ("raw", "message"),
        [
            ([{"point_id": "D1", "option_id": "O1"}], "D2 is not answered"),
            (
                [{"point_id": "D1", "option_id": "Z"}, {"point_id": "D2", "text": "x"}],
                "no such option",
            ),
            (
                [{"point_id": "D1"}, {"point_id": "D2", "text": "x"}],
                "choose an option or write",
            ),
            (
                [
                    {"point_id": "D1", "option_id": "O1"},
                    {"point_id": "D2", "text": "x"},
                    {"point_id": "D3", "text": "y"},
                ],
                "no such point: D3",
            ),
            ("x", "must be a list"),
        ],
    )
    def test_answers_that_do_not_fit_are_refused(self, raw, message):
        with pytest.raises(InvalidDecision, match=message):
            answers_from_data(raw, self.points)


class TestBook:
    def book(self) -> DecisionBook:
        return DecisionBook(wall_clock=lambda: NOW)

    def propose(self, book: DecisionBook, **overrides):
        args = {
            "proposer": "interactive",
            "title": "How to store decisions",
            "points": points_from_data(RAW_POINTS),
            "session_id": "s-1",
        }
        args.update(overrides)
        return book.propose(TASK, **args)

    def test_a_proposed_decision_is_pending_and_belongs_to_the_task(self):
        book = self.book()

        decision = self.propose(book)

        found = book.get(decision.id)
        assert (found.status, found.task_id, found.branch) == (
            "pending",
            "task-a",
            "devon/a",
        )
        assert (found.proposer, found.session_id, found.answers) == (
            "interactive",
            "s-1",
            (),
        )
        assert [d.id for d in book.for_task("task-a")] == [decision.id]
        assert book.for_task("other") == []

    def test_answering_records_the_answers_and_when(self):
        book = self.book()
        decision = self.propose(book)

        answered = book.answer(
            decision.id,
            [{"point_id": "D1", "option_id": "O1"}, {"point_id": "D2", "text": "No."}],
        )

        assert answered.status == "answered"
        assert answered.answered_at is not None and answered.delivered_at is None
        text = describe_answers(answered)
        assert "Chosen: O1 (In the store)" in text
        assert "In the user's words: No." in text

    def test_a_decision_is_answered_once(self):
        book = self.book()
        decision = self.propose(book)
        answers = [
            {"point_id": "D1", "option_id": "O1"},
            {"point_id": "D2", "text": "x"},
        ]
        book.answer(decision.id, answers)

        with pytest.raises(AlreadyAnswered):
            book.answer(decision.id, answers)

    def test_an_answer_that_does_not_fit_leaves_it_pending(self):
        book = self.book()
        decision = self.propose(book)

        with pytest.raises(InvalidDecision):
            book.answer(decision.id, [{"point_id": "D1", "option_id": "O1"}])

        assert book.get(decision.id).status == "pending"

    def test_delivery_is_recorded_once(self):
        book = self.book()
        decision = self.propose(book)

        book.mark_delivered(decision.id)

        assert book.get(decision.id).delivered_at is not None

    def test_a_deleted_decision_is_gone_and_cannot_be_answered(self):
        book = self.book()
        decision = self.propose(book)

        assert book.delete(decision.id).id == decision.id

        with pytest.raises(NoSuchDecision):
            book.get(decision.id)
        with pytest.raises(NoSuchDecision):
            book.answer(decision.id, [])

    def test_a_decision_needs_a_title(self):
        with pytest.raises(InvalidDecision, match="title"):
            self.propose(self.book(), title="  ")

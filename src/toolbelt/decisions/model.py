"""What an agent proposes and the user answers.

A *decision* is a set of *points* about one task. Each point has a context and
any number of options, each with what picking it means (summary), its context,
benefits, costs and risks, and optionally a recommendation. The user answers
every point with one of its options, free text, or both.

The shapes are plain dataclasses so that reading them back from the database
stays cheap at start-up; ``points_from_json`` validates what an agent sent.
"""

import json
from dataclasses import asdict, dataclass
from typing import Any


class InvalidDecision(ValueError):
    """What was proposed or answered does not fit the shape."""


@dataclass(frozen=True)
class DecisionOption:
    id: str
    title: str
    #: What picking it means, in a sentence or two: always shown.
    summary: str = ""
    #: More context, shown on request.
    context: str = ""
    benefits: str = ""
    costs: str = ""
    risks: str = ""


@dataclass(frozen=True)
class Recommendation:
    #: The id of one of the point's options, or ``None`` for advice that is not
    #: one of them.
    option_id: str | None
    reasoning: str


@dataclass(frozen=True)
class DecisionPoint:
    id: str
    title: str
    #: Background: what it is about and what was checked.
    context: str = ""
    options: tuple[DecisionOption, ...] = ()
    recommendation: Recommendation | None = None


@dataclass(frozen=True)
class PointAnswer:
    point_id: str
    #: The option chosen, if one was.
    option_id: str | None = None
    #: Whatever else was typed (a point with no option is answered by this alone).
    text: str = ""


@dataclass(frozen=True)
class Decision:
    id: str
    task_id: str
    repo_id: int
    branch: str
    #: ``interactive`` (a session the user is in) or ``autonomous`` (one of
    #: toolbelt's own agent runs).
    proposer: str
    session_id: str | None
    run_id: str | None
    title: str
    points: tuple[DecisionPoint, ...]
    #: ``pending`` until answered.
    status: str
    answers: tuple[PointAnswer, ...] = ()
    created_at: str = ""
    answered_at: str | None = None
    #: When the answer reached the agent (an interactive tool call returned, or
    #: an autonomous run was resumed with it).
    delivered_at: str | None = None


def _text(raw: dict, key: str, where: str, *, required: bool = False) -> str:
    value = raw.get(key, "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise InvalidDecision(f"{where}: {key} must be text")
    if required and not value.strip():
        raise InvalidDecision(f"{where}: {key} is required")
    return value


def _option(raw: Any, where: str) -> DecisionOption:
    if not isinstance(raw, dict):
        raise InvalidDecision(f"{where}: an option must be an object")
    return DecisionOption(
        id=_text(raw, "id", where, required=True),
        title=_text(raw, "title", where, required=True),
        summary=_text(raw, "summary", where),
        context=_text(raw, "context", where),
        benefits=_text(raw, "benefits", where),
        costs=_text(raw, "costs", where),
        risks=_text(raw, "risks", where),
    )


def _point(raw: Any, where: str) -> DecisionPoint:
    if not isinstance(raw, dict):
        raise InvalidDecision(f"{where}: a point must be an object")
    point_id = _text(raw, "id", where, required=True)
    where = f"point {point_id}"
    options_raw = raw.get("options") or []
    if not isinstance(options_raw, list):
        raise InvalidDecision(f"{where}: options must be a list")
    options = tuple(
        _option(o, f"{where}, option {i + 1}") for i, o in enumerate(options_raw)
    )
    if len({o.id for o in options}) != len(options):
        raise InvalidDecision(f"{where}: option ids must be unique")
    recommendation = None
    advice = raw.get("recommendation")
    if advice is not None:
        if not isinstance(advice, dict):
            raise InvalidDecision(f"{where}: recommendation must be an object")
        option_id = advice.get("option_id")
        if option_id is not None and option_id not in {o.id for o in options}:
            raise InvalidDecision(
                f"{where}: the recommendation names an option that is not there"
            )
        recommendation = Recommendation(
            option_id=option_id,
            reasoning=_text(advice, "reasoning", f"{where} recommendation"),
        )
    return DecisionPoint(
        id=point_id,
        title=_text(raw, "title", where, required=True),
        context=_text(raw, "context", where),
        options=options,
        recommendation=recommendation,
    )


def points_from_data(raw: Any) -> tuple[DecisionPoint, ...]:
    """The points an agent sent (a list of objects), checked."""
    if not isinstance(raw, list) or not raw:
        raise InvalidDecision("points must be a non-empty list")
    points = tuple(_point(p, f"point {i + 1}") for i, p in enumerate(raw))
    if len({p.id for p in points}) != len(points):
        raise InvalidDecision("point ids must be unique")
    return points


def points_to_json(points: tuple[DecisionPoint, ...]) -> str:
    return json.dumps([asdict(p) for p in points])


def points_from_json(text: str) -> tuple[DecisionPoint, ...]:
    return tuple(
        DecisionPoint(
            id=p["id"],
            title=p["title"],
            context=p["context"],
            options=tuple(DecisionOption(**o) for o in p["options"]),
            recommendation=Recommendation(**p["recommendation"])
            if p["recommendation"]
            else None,
        )
        for p in json.loads(text)
    )


def answers_to_json(answers: tuple[PointAnswer, ...]) -> str:
    return json.dumps([asdict(a) for a in answers])


def answers_from_json(text: str | None) -> tuple[PointAnswer, ...]:
    return tuple(PointAnswer(**a) for a in json.loads(text)) if text else ()


def answers_from_data(
    raw: Any, points: tuple[DecisionPoint, ...]
) -> tuple[PointAnswer, ...]:
    """The answers a user sent, checked against ``points``: one per point, each
    an option of that point, text, or both, and never neither."""
    if not isinstance(raw, list):
        raise InvalidDecision("answers must be a list")
    by_point: dict[str, PointAnswer] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise InvalidDecision("an answer must be an object")
        sent = PointAnswer(
            point_id=str(item.get("point_id", "")),
            option_id=item.get("option_id") or None,
            text=str(item.get("text") or "").strip(),
        )
        by_point[sent.point_id] = sent
    ordered: list[PointAnswer] = []
    for point in points:
        answer = by_point.pop(point.id) if point.id in by_point else None
        if answer is None:
            raise InvalidDecision(f"point {point.id} is not answered")
        if answer.option_id is not None and answer.option_id not in {
            o.id for o in point.options
        }:
            raise InvalidDecision(f"point {point.id}: no such option")
        if answer.option_id is None and not answer.text:
            raise InvalidDecision(
                f"point {point.id}: choose an option or write an answer"
            )
        ordered.append(answer)
    if by_point:
        raise InvalidDecision(f"no such point: {', '.join(sorted(by_point))}")
    return tuple(ordered)


def describe_answers(decision: Decision) -> str:
    """The answers as text an agent can read (the tool result, or the prompt
    that resumes a run): each point with what was chosen and said."""
    lines = [f"Decision: {decision.title}"]
    answers = {a.point_id: a for a in decision.answers}
    for point in decision.points:
        answer = answers.get(point.id)
        lines.append(f"- {point.id}: {point.title}")
        if answer is None:
            lines.append("  (not answered)")
            continue
        if answer.option_id is not None:
            option = next((o for o in point.options if o.id == answer.option_id), None)
            lines.append(
                f"  Chosen: {answer.option_id}"
                + (f" ({option.title})" if option is not None else "")
            )
        if answer.text:
            lines.append(f"  In the user's words: {answer.text}")
    return "\n".join(lines)

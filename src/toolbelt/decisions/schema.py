"""The shapes an agent sends when it proposes decisions or the user sends when
answering, as pydantic models: the input schemas of the MCP tools (the server
an interactive session uses, and the one built into an autonomous run)."""

from pydantic import BaseModel


class OptionInput(BaseModel):
    id: str
    title: str
    summary: str = ""
    context: str = ""
    benefits: str = ""
    costs: str = ""
    risks: str = ""


class RecommendationInput(BaseModel):
    option_id: str | None = None
    reasoning: str = ""


class PointInput(BaseModel):
    id: str
    title: str
    context: str = ""
    options: list[OptionInput] = []
    recommendation: RecommendationInput | None = None


class AnswerInput(BaseModel):
    point_id: str
    option_id: str | None = None
    text: str = ""


def proposal_schema() -> dict:
    """The JSON Schema of an autonomous run's ``propose_decisions`` input: a
    title and points (the task is the run's own)."""
    from pydantic import TypeAdapter

    points = TypeAdapter(list[PointInput]).json_schema()
    definitions = points.pop("$defs", {})
    return {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "What the decision is about."},
            "points": points,
        },
        "required": ["title", "points"],
        "$defs": definitions,
    }

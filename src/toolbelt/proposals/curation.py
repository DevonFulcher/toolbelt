"""What the curator agent is told: it reads every decision, with the reasons the
user gave, and writes the guidelines the proposer is given."""

from collections.abc import Sequence

from toolbelt.proposals.instructions import _history_line
from toolbelt.proposals.model import Guidelines, Proposal

#: Changes whenever the wording does, like ``instructions.PROMPT_VERSION``.
CURATOR_VERSION = "1"


def build_curator_instructions(
    decided: Sequence[Proposal], previous: Guidelines | None
) -> str:
    lines = "\n".join(_history_line(p) for p in decided)
    before = (
        f"\n## The guidelines as they stand ({previous.author}, kept unless the "
        f"decisions contradict them)\n\n{previous.body}\n"
        if previous and previous.body.strip()
        else ""
    )
    return f"""\
You maintain the guidelines a proposer agent follows when it proposes software \
work to Devon Fulcher. Below is every proposal Devon has decided, approved or \
rejected, with the tags and reasons he gave for rejections.

Write the guidelines: a short list (at most 15 lines) of concrete, general \
statements about what Devon wants proposed and what he does not, each \
supported by several decisions. Prefer patterns that repeat to one-off \
reactions; say what kinds of work he approves and what he rejects and why; \
include limits on size or scope if he keeps rejecting for them. Do not mention \
individual proposals by name. Keep any statement in the existing guidelines \
that the decisions still support.

Answer with the guidelines text only, as plain lines starting with "- ".

## Decisions

{lines}
{before}"""

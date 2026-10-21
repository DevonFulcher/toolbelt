"""Who to ask for a review: the ``slack.assign`` rules of the config.

A post goes to whoever the first matching rule names, or the one asked longest
ago among several. A bump asks someone else: any reviewer other than who was
asked before, again the one asked longest ago. Only people in ``reviewers`` can
be chosen; the person who posts can always change the choice before sending.
"""

from datetime import UTC, datetime

from toolbelt.git.stack.store import SlackPostRow
from toolbelt.ui.slack_compose import Person
from toolbelt.ui.slack_config import AssignRule, SlackConfig

_NEVER = datetime.min.replace(tzinfo=UTC)


def _matches(rule: AssignRule, repo: str, lines: int) -> bool:
    if rule.repo is not None and rule.repo.lower() not in (
        repo.lower(),
        repo.lower().split("/")[-1],
    ):
        return False
    if rule.min_lines is not None and lines < rule.min_lines:
        return False
    return rule.max_lines is None or lines < rule.max_lines


def _longest_ago(names: list[str], history: list[SlackPostRow]) -> str | None:
    """Of ``names``, the one asked longest ago (never asked first, then in the
    order given)."""
    if not names:
        return None
    last: dict[str, datetime] = {}
    for row in history:
        if row.assignee_name:
            last[row.assignee_name] = max(
                last.get(row.assignee_name, _NEVER), row.posted_at
            )
    return min(names, key=lambda name: last.get(name, _NEVER))


def choose_assignee(
    config: SlackConfig,
    *,
    repo: str,
    lines: int,
    history: list[SlackPostRow],
    bump: bool = False,
    exclude: frozenset[str] = frozenset(),
) -> Person | None:
    """``repo`` is ``owner/name``; ``lines`` the PR's added plus deleted lines;
    ``history`` every recorded post; ``exclude`` names not to choose (for a
    bump, who was asked before)."""
    known = {person.name: person for person in config.reviewers}
    if bump:
        candidates = list(known)
    else:
        candidates = []
        for rule in config.rules:
            if _matches(rule, repo, lines):
                candidates = [rule.to] if rule.to else list(rule.rotate)
                break
        else:
            candidates = [config.default] if config.default else list(known)
    name = _longest_ago(
        [c for c in candidates if c in known and c not in exclude], history
    )
    return known[name] if name else None

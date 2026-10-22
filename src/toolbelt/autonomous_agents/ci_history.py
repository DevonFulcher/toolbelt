"""Why a PR's CI keeps failing, in a sentence for the card.

The guard that leaves a PR to the user (``eligibility.decide``, reason
``thrash``) counts the commits whose CI ended in failure over the PR's whole
life. That number says nothing about *why*: a PR whose parent branch is merged
into it on every sync gets a new commit, and a new CI run, each time, and a
flaky check fails some of them. ``explain`` reads the last commits of the PR
with the checks that failed on each and says which checks fail, whether the
failures persist or come and go, and how many of the commits were merges of
other branches rather than changes of the PR's own.

A commit's failing checks are those of the newest run of each workflow only
(``pr_fetch.current_contexts``: a rerun supersedes the runs before it), as on
the card; a commit GitHub rolls up as failed whose newest runs all passed
failed only in a run a rerun replaced.
"""

from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from toolbelt.ui.pr_fetch import current_contexts

#: How many of the PR's newest commits are read.
COMMITS = 40

#: How many failing checks the sentence names.
NAMED_CHECKS = 3

CI_HISTORY_QUERY = f"""
query($owner: String!, $repo: String!, $number: Int!) {{
  repository(owner: $owner, name: $repo) {{
    pullRequest(number: $number) {{
      commits(last: {COMMITS}) {{ nodes {{ commit {{
        oid
        parents {{ totalCount }}
        statusCheckRollup {{ state contexts(first: 100) {{ nodes {{
          __typename
          ... on CheckRun {{
            name conclusion status startedAt
            checkSuite {{ workflowRun {{ databaseId createdAt workflow {{ name }} }} }}
          }}
          ... on StatusContext {{ context state }}
        }} }} }}
      }} }} }}
    }}
  }}
}}
"""

_FAILED_ROLLUPS = {"FAILURE", "ERROR"}
_FAILED_OUTCOMES = {"FAILURE", "ERROR", "TIMED_OUT", "ACTION_REQUIRED", "CANCELLED"}


@dataclass(frozen=True)
class CommitCi:
    """One commit of the PR and how its CI went."""

    oid: str
    #: A merge of another branch (usually the parent), not a change of its own.
    merge: bool
    #: GitHub's rollup state (``None``: no CI ran).
    rollup: str | None
    #: The checks that failed in the newest run of each workflow.
    failing: tuple[str, ...]


def parse_history(payload: dict) -> list[CommitCi]:
    """The PR's commits, oldest first, from ``CI_HISTORY_QUERY``'s answer."""
    nodes = payload["data"]["repository"]["pullRequest"]["commits"]["nodes"]
    commits = []
    for node in nodes:
        commit = node["commit"]
        rollup = commit["statusCheckRollup"]
        failing = ()
        if rollup:
            failing = tuple(
                sorted(
                    {
                        context.get("name") or context["context"]
                        for context in current_contexts(rollup["contexts"]["nodes"])
                        if (context.get("conclusion") or context.get("state"))
                        in _FAILED_OUTCOMES
                    }
                )
            )
        commits.append(
            CommitCi(
                oid=commit["oid"],
                merge=commit["parents"]["totalCount"] > 1,
                rollup=rollup["state"] if rollup else None,
                failing=failing,
            )
        )
    return commits


def _trailing_failures(commits: list[CommitCi]) -> list[CommitCi]:
    """The newest commits in a row whose newest runs still have a failing check."""
    tail: list[CommitCi] = []
    for commit in reversed(commits):
        if not commit.failing:
            break
        tail.append(commit)
    return tail


def explain(commits: list[CommitCi]) -> str | None:
    """A few sentences on why CI keeps failing; ``None`` when no commit's CI
    failed."""
    judged = [commit for commit in commits if commit.rollup is not None]
    failed = [commit for commit in judged if commit.rollup in _FAILED_ROLLUPS]
    if not failed:
        return None
    sentences = [f"CI failed on {len(failed)} of the last {len(judged)} commits"]
    named = Counter(name for commit in failed for name in commit.failing)
    if named:
        top = ", ".join(
            f"{name} ({count})" for name, count in named.most_common(NAMED_CHECKS)
        )
        sentences[0] += f", most often in {top}"
    sentences[0] += "."
    tail = _trailing_failures(commits)
    shared = (
        set.intersection(*(set(commit.failing) for commit in tail)) if tail else set()
    )
    passed = sum(1 for commit in judged if commit.rollup == "SUCCESS")
    if len(tail) >= 2 and shared:
        sentences.append(
            f"The last {len(tail)} commits all fail in {', '.join(sorted(shared))}: "
            "a failure that persists and needs a fix."
        )
    elif passed:
        sentences.append(
            f"CI passed on {passed} other commits, so the failures come and go: "
            "more likely flaky or infrastructure than a fix that does not converge."
        )
    merges = sum(1 for commit in commits if commit.merge)
    if merges:
        sentences.append(
            f"{merges} of the last {len(commits)} commits are merges of other "
            "branches, not changes of its own."
        )
    return " ".join(sentences)


#: (repo checkout, PR number) -> the explanation, or ``None``.
type CiExplainer = Callable[[Path, int], Awaitable[str | None]]


def ci_explainer(
    graphql: Callable[[str, Mapping[str, str | int]], Awaitable[dict]],
    slug_of: Callable[[Path], Awaitable[str | None]],
) -> CiExplainer:
    """The explanation of a PR's CI, read through ``graphql`` (a query and its
    variables) for the repo checked out at a path (``slug_of`` gives its
    ``owner/name``)."""

    async def run(repo_path: Path, number: int) -> str | None:
        slug = await slug_of(repo_path)
        if slug is None:
            return None
        owner, repo = slug.split("/", 1)
        payload = await graphql(
            CI_HISTORY_QUERY, {"owner": owner, "repo": repo, "number": number}
        )
        return explain(parse_history(payload))

    return run

"""`unlink_github_stack` finds the PR's GitHub stack and unstacks it (`gh` faked)."""

import json
import subprocess

import pytest
import typer

from toolbelt.git.stack import gh_stack


def _stack(number: int, *prs: int) -> dict:
    return {"number": number, "pull_requests": [{"number": pr} for pr in prs]}


class Gh:
    def __init__(self, stacks: list[dict], *, unstack_removes: bool = True) -> None:
        self.stacks = stacks
        self.unstack_removes = unstack_removes
        self.unstacked: list[list[str]] = []

    def capture(self, cmd, **kwargs):
        if cmd[:3] == ["gh", "pr", "view"]:
            return json.dumps({"number": 15})
        assert cmd[:3] == ["gh", "api", "--paginate"]
        return json.dumps([self.stacks])

    def run(self, cmd, **kwargs):
        assert cmd[:3] == ["gh", "stack", "unstack"]
        self.unstacked.append(cmd)
        if self.unstack_removes:
            self.stacks = [s for s in self.stacks if s["number"] != int(cmd[3])]
        return subprocess.CompletedProcess(cmd, 0)


def _install(monkeypatch: pytest.MonkeyPatch, gh: Gh) -> None:
    monkeypatch.setattr(gh_stack, "capture", gh.capture)
    monkeypatch.setattr(gh_stack.logged_process, "run", gh.run)


def test_unstacks_the_stack_holding_the_pr(monkeypatch: pytest.MonkeyPatch):
    gh = Gh([_stack(3, 1, 2), _stack(17, 14, 15, 16)])
    _install(monkeypatch, gh)

    gh_stack.unlink_github_stack("devon/feature")

    assert gh.unstacked == [["gh", "stack", "unstack", "17"]]


def test_a_pr_outside_any_stack_is_left_alone(monkeypatch: pytest.MonkeyPatch):
    gh = Gh([_stack(3, 1, 2)])
    _install(monkeypatch, gh)

    gh_stack.unlink_github_stack("15")

    assert gh.unstacked == []


def test_a_stack_github_keeps_is_an_error(monkeypatch: pytest.MonkeyPatch):
    gh = Gh([_stack(17, 14, 15)], unstack_removes=False)
    _install(monkeypatch, gh)

    with pytest.raises(typer.Exit):
        gh_stack.unlink_github_stack("15")

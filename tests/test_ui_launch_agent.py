from types import SimpleNamespace

from toolbelt.ui.launch_agent import UI_LAUNCH_AGENT_LABEL, restart_ui_launch_agent


class FakeRun:
    def __init__(self, loaded: bool) -> None:
        self.loaded = loaded
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], **_kwargs: object) -> SimpleNamespace:
        self.calls.append(args)
        is_print = args[:2] == ["launchctl", "print"]
        return SimpleNamespace(returncode=0 if self.loaded or not is_print else 113)


def test_restarts_a_loaded_agent() -> None:
    run, said = FakeRun(loaded=True), list[str]()
    assert restart_ui_launch_agent(said.append, run=run) is True
    assert run.calls[-1][:3] == ["launchctl", "kickstart", "-k"]
    assert run.calls[-1][3].endswith(f"/{UI_LAUNCH_AGENT_LABEL}")
    assert said == [f"restarted {UI_LAUNCH_AGENT_LABEL}"]


def test_leaves_an_unloaded_agent_alone() -> None:
    run, said = FakeRun(loaded=False), list[str]()
    assert restart_ui_launch_agent(said.append, run=run) is False
    assert [c[1] for c in run.calls] == ["print"]
    assert said == []

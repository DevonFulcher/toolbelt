from dataclasses import dataclass
from typing import Generic, Literal, TypeVar

import typer
from textual import events
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, Footer, Header, Input, Label, ListItem, ListView

from toolbelt.linear.client import LinearIssue, LinearTeam
from toolbelt.logger import logger

TChoice = TypeVar("TChoice")


@dataclass(frozen=True)
class IssueSelection:
    kind: Literal["issue", "create_new"]
    identifier: str | None = None


class _ChoiceItem(ListItem, Generic[TChoice]):
    def __init__(self, label: str, *, choice: TChoice) -> None:
        super().__init__(Label(label))
        self.choice = choice


class _PickerApp(App[TChoice], Generic[TChoice]):
    CSS = """
    ListView {
        height: 1fr;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("escape", "quit", "Quit"),
    ]

    def __init__(
        self,
        *,
        title: str,
        items: list[tuple[str, TChoice]],
    ) -> None:
        super().__init__()
        self._title = title
        self._items = items

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield ListView()
        yield Footer()

    def on_mount(self) -> None:
        self.title = self._title
        list_view = self.query_one(ListView)
        for label, choice in self._items:
            list_view.append(_ChoiceItem(label, choice=choice))
        list_view.focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, _ChoiceItem):
            self.exit(item.choice)

    def on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            list_view = self.query_one(ListView)
            highlighted = list_view.highlighted_child
            if isinstance(highlighted, _ChoiceItem):
                self.exit(highlighted.choice)


def pick_backlog_issue(*, issues: list[LinearIssue]) -> IssueSelection:
    items: list[tuple[str, IssueSelection]] = [
        (
            f"{i.identifier} — {i.title}",
            IssueSelection(kind="issue", identifier=i.identifier),
        )
        for i in issues
    ]
    items.append(("Create new Linear ticket", IssueSelection(kind="create_new")))
    result = _PickerApp[IssueSelection](
        title="Select a Linear task to start",
        items=items,
    ).run()
    if result is None:
        logger.error("No selection made.")
        raise typer.Exit(1)
    return result


def pick_team(*, teams: list[LinearTeam]) -> LinearTeam:
    items = [(f"{t.key} — {t.name}", t) for t in teams]
    result = _PickerApp[LinearTeam](title="Select a Linear team", items=items).run()
    if result is None:
        logger.error("No selection made.")
        raise typer.Exit(1)
    return result


class _ConfirmTitleApp(App[str]):
    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("q", "cancel", "Cancel"),
    ]

    def __init__(self, *, initial_title: str) -> None:
        super().__init__()
        self._initial_title = initial_title

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Label("Review/edit the Linear ticket title, then confirm.")
        yield Input(value=self._initial_title, id="title_input")
        with Horizontal():
            yield Button("Confirm", id="confirm", variant="success")
            yield Button("Cancel", id="cancel", variant="error")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Confirm ticket title"
        self.query_one("#title_input", Input).focus()

    def action_cancel(self) -> None:
        self.exit(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        match event.button.id:
            case "confirm":
                title = self.query_one("#title_input", Input).value.strip()
                self.exit(title)
            case "cancel":
                self.exit(None)
            case _:
                return

    def on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            title = self.query_one("#title_input", Input).value.strip()
            self.exit(title)


def confirm_and_edit_title(*, initial_title: str) -> str:
    result = _ConfirmTitleApp(initial_title=initial_title).run()
    if result is None:
        logger.error("Title confirmation cancelled.")
        raise typer.Exit(1)
    if not result.strip():
        logger.error("Title must not be empty.")
        raise typer.Exit(1)
    return result.strip()

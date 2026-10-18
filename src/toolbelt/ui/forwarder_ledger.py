"""What toolbelt knows it created for webhook forwarding, kept across restarts.

GitHub's record of a forwarded webhook does not say who made it, and a repo
can hold only one. So the forwarders (``webhook_forwarders.py``) write down
what they are certain is theirs:

* the ids of the webhooks they created, so a webhook left behind inactive by a
  forwarder that died can be told from one somebody else made (only the first
  is ever deleted);
* the pid of each forwarder process they started, so one that outlived a
  server that was killed can be stopped at the next start.

``SettingsLedger`` keeps it as JSON under one key of the stack DB's settings;
``MemoryLedger`` is for tests.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from toolbelt.git.stack.store import StackStore, default_db_path

SETTING = "webhook_forwarders"


class Ledger(Protocol):
    def hooks(self, slug: str) -> set[int]:
        """The webhooks of ``slug`` that toolbelt created."""
        ...

    def add_hook(self, slug: str, hook_id: int) -> None: ...

    def forget_hook(self, slug: str, hook_id: int) -> None: ...

    def pids(self) -> dict[str, int]:
        """The forwarder process started for each repo, by ``owner/name``."""
        ...

    def set_pid(self, slug: str, pid: int) -> None: ...

    def clear_pid(self, slug: str) -> None: ...


class MemoryLedger:
    def __init__(self) -> None:
        self._hooks: dict[str, set[int]] = {}
        self._pids: dict[str, int] = {}

    def hooks(self, slug: str) -> set[int]:
        return set(self._hooks.get(slug, ()))

    def add_hook(self, slug: str, hook_id: int) -> None:
        self._hooks.setdefault(slug, set()).add(hook_id)

    def forget_hook(self, slug: str, hook_id: int) -> None:
        self._hooks.get(slug, set()).discard(hook_id)

    def pids(self) -> dict[str, int]:
        return dict(self._pids)

    def set_pid(self, slug: str, pid: int) -> None:
        self._pids[slug] = pid

    def clear_pid(self, slug: str) -> None:
        self._pids.pop(slug, None)


class SettingsLedger:
    """A ``Ledger`` in the stack DB; each call opens its own connection."""

    def __init__(self, db_path: Callable[[], Path] = default_db_path) -> None:
        self._db_path = db_path

    def _read(self, store: StackStore) -> dict:
        data = json.loads(store.get_setting(SETTING, "{}"))
        return {"hooks": data.get("hooks", {}), "pids": data.get("pids", {})}

    def _update(self, change: Callable[[dict], None]) -> None:
        store = StackStore(self._db_path())
        try:
            data = self._read(store)
            change(data)
            store.set_setting(SETTING, json.dumps(data, separators=(",", ":")))
        finally:
            store.close()

    def _get(self) -> dict:
        store = StackStore(self._db_path())
        try:
            return self._read(store)
        finally:
            store.close()

    def hooks(self, slug: str) -> set[int]:
        return set(self._get()["hooks"].get(slug, ()))

    def add_hook(self, slug: str, hook_id: int) -> None:
        def change(data: dict) -> None:
            ids = set(data["hooks"].get(slug, ()))
            ids.add(hook_id)
            data["hooks"][slug] = sorted(ids)

        self._update(change)

    def forget_hook(self, slug: str, hook_id: int) -> None:
        def change(data: dict) -> None:
            ids = set(data["hooks"].get(slug, ()))
            ids.discard(hook_id)
            if ids:
                data["hooks"][slug] = sorted(ids)
            else:
                data["hooks"].pop(slug, None)

        self._update(change)

    def pids(self) -> dict[str, int]:
        return dict(self._get()["pids"])

    def set_pid(self, slug: str, pid: int) -> None:
        self._update(lambda data: data["pids"].__setitem__(slug, pid))

    def clear_pid(self, slug: str) -> None:
        self._update(lambda data: data["pids"].pop(slug, None))

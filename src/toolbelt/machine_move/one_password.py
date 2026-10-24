"""The 1Password side of the archive, behind a seam.

``DocumentVault`` is what export and import need from 1Password; ``OpCli`` is
the one implementation, driving the ``op`` CLI. Tests give the commands a fake
``op`` executable through ``$TOOLBELT_OP`` (the binary to run, default ``op``)
or pass their own ``DocumentVault``.
"""

import json
import os
from pathlib import Path
from typing import Protocol

from toolbelt import logged_process
from toolbelt.machine_move.archive_layout import MachineMoveError

OP_BINARY_ENV_VAR = "TOOLBELT_OP"


class DocumentVault(Protocol):
    def ensure_signed_in(self) -> None: ...

    def store(self, path: Path, *, title: str) -> str:
        """Save ``path`` as the document ``title`` (replacing a previous
        archive of that title); returns a description of where it went."""
        ...

    def fetch(self, title: str, *, to: Path) -> None: ...


class OpCli:
    def __init__(self, *, vault: str | None = None, binary: str | None = None) -> None:
        self._vault = vault
        self._binary = binary or os.environ.get(OP_BINARY_ENV_VAR, "op")

    def _op(self, *args: str) -> str:
        try:
            result = logged_process.run(
                [self._binary, *args], capture_output=True, text=True
            )
        except FileNotFoundError as error:
            raise MachineMoveError(
                f"the 1Password CLI ({self._binary}) is not installed: "
                "brew install 1password-cli"
            ) from error
        if result.returncode != 0:
            raise MachineMoveError(
                f"`op {args[0]} {args[1] if len(args) > 1 else ''}` failed: "
                f"{result.stderr.strip()}"
            )
        return result.stdout

    def _vault_args(self) -> list[str]:
        return ["--vault", self._vault] if self._vault else []

    def ensure_signed_in(self) -> None:
        try:
            self._op("whoami")
        except MachineMoveError as error:
            if "not installed" in str(error):
                raise
            raise MachineMoveError(
                "the 1Password CLI is not signed in. Open the 1Password app, turn on "
                "Settings > Developer > Integrate with the 1Password CLI (or run "
                "`eval $(op signin)`), then run this again."
            ) from error

    def store(self, path: Path, *, title: str) -> str:
        listing = json.loads(
            self._op(
                "item",
                "list",
                "--categories",
                "Document",
                "--format",
                "json",
                *self._vault_args(),
            )
            or "[]"
        )
        existing = [item["id"] for item in listing if item.get("title") == title]
        if existing:
            self._op("document", "edit", existing[0], str(path), "--title", title)
            return f'1Password document "{title}" (updated)'
        self._op("document", "create", str(path), "--title", title, *self._vault_args())
        return f'1Password document "{title}" (created)'

    def fetch(self, title: str, *, to: Path) -> None:
        self._op(
            "document",
            "get",
            title,
            "--out-file",
            str(to),
            "--force",
            *self._vault_args(),
        )

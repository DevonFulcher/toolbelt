import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def chdir(path: Path) -> Iterator[None]:
    """Temporarily change the working directory within a context."""
    prev = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(prev)

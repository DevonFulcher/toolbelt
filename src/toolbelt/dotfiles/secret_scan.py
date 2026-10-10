"""Heuristic scan for secret-looking content, used before publishing dotfiles.

Findings never include the matched text, so output is safe to paste. A line
containing ``tt:allow-secret`` is skipped, for deliberate false positives.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from toolbelt import logged_process

ALLOW_MARKER = "tt:allow-secret"
_MAX_BYTES = 2 * 1024 * 1024

_RULES: dict[str, re.Pattern[str]] = {
    "private key block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "AWS access key id": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "GitHub token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_\w{22,})"),
    "Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    "API key (sk-...)": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "JSON web token": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\."),
    "credential in URL": re.compile(r"://[^/\s:@]+:[^/\s:@$]{6,}@"),
    "secret assignment": re.compile(
        r"""(?ix)
        \b[\w.-]*(?:api[_-]?key|secret|token|passw(?:or)?d)[\w.-]*
        ["']?\s*[:=]\s*["']?
        (?![$<{(])               # not a variable reference or placeholder
        [A-Za-z0-9/+_=-]{16,}
        """
    ),
}


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    rule: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}"


def scan_text(text: str, *, path: Path) -> list[Finding]:
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if ALLOW_MARKER in line:
            continue
        for rule, pattern in _RULES.items():
            if pattern.search(line):
                findings.append(Finding(path, number, rule))
    return findings


def scan_file(path: Path) -> list[Finding]:
    if path.is_symlink() or path.stat().st_size > _MAX_BYTES:
        return []
    data = path.read_bytes()
    if b"\0" in data[:8192]:
        return []
    return scan_text(data.decode(errors="replace"), path=path)


def scan_paths(paths: list[Path]) -> list[Finding]:
    """Scan files, recursing into directories."""
    findings: list[Finding] = []
    for path in paths:
        if path.is_dir() and not path.is_symlink():
            children = sorted(path.rglob("*"))
            findings += scan_paths([c for c in children if not c.is_dir()])
        else:
            findings += scan_file(path)
    return findings


def scan_repo(root: Path) -> list[Finding]:
    """Scan every file git would commit: tracked, plus untracked-not-ignored."""
    listed = logged_process.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        capture_output=True,
        check=True,
    ).stdout.decode()
    files = [root / name for name in listed.split("\0") if name]
    return scan_paths([f for f in files if f.exists()])

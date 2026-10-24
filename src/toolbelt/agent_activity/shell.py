"""Reads a shell command well enough to say what it is: the programs of its
chain, the subcommand of tools that have them, and a category.

Not a shell: substitutions (``$(...)``), heredoc bodies, aliases and computed
paths are left as written. The plugin does the same split to attribute a
command to a worktree (``claude-plugin/hooks/register.ts``); what a command
*is* is decided here, at ingest, so it can improve and be re-run over what is
stored.
"""

from dataclasses import dataclass
from typing import Literal

type Category = Literal[
    "edit",
    "read",
    "search",
    "files",
    "vcs",
    "github",
    "test",
    "build",
    "lint",
    "deps",
    "run",
    "network",
    "infra",
    "toolbelt",
    "subagent",
    "web",
    "mcp",
    "other",
]

#: Every category, in the order charts stack them.
CATEGORIES: tuple[str, ...] = (
    "edit",
    "read",
    "search",
    "files",
    "vcs",
    "github",
    "test",
    "build",
    "lint",
    "deps",
    "run",
    "network",
    "infra",
    "toolbelt",
    "subagent",
    "web",
    "mcp",
    "other",
)

#: What a tool that is not Bash is.
TOOL_CATEGORIES: dict[str, str] = {
    "Edit": "edit",
    "Write": "edit",
    "NotebookEdit": "edit",
    "MultiEdit": "edit",
    "Read": "read",
    "NotebookRead": "read",
    "Grep": "search",
    "Glob": "search",
    "WebFetch": "web",
    "WebSearch": "web",
    "Task": "subagent",
    "Agent": "subagent",
}

# Commands that carry no meaning of their own in a chain.
_NAVIGATION = {
    "cd",
    "pushd",
    "popd",
    "echo",
    "printf",
    "true",
    "false",
    ":",
    "export",
    "set",
    "source",
    ".",
    "exit",
    "unset",
    "alias",
    "wait",
}
_WRAPPERS = {
    "sudo",
    "time",
    "env",
    "command",
    "nohup",
    "exec",
    "nice",
    "caffeinate",
    "stdbuf",
    "timeout",
    "xargs",
}
# Programs that read what an earlier command wrote: not what the command is.
_FILTERS = {
    "head",
    "tail",
    "wc",
    "sort",
    "uniq",
    "tee",
    "tr",
    "cut",
    "column",
    "less",
    "more",
}
# Options that take the next word, for the programs that have subcommands.
_VALUE_OPTIONS = {
    "-C",
    "-c",
    "--dir",
    "--directory",
    "--project",
    "--cwd",
    "--prefix",
    "--workdir",
    "--config",
    "-p",
    "--package",
    "-f",
    "--file",
    "-w",
    "--filter",
}
_WITH_SUBCOMMANDS = {
    "git",
    "gh",
    "uv",
    "pnpm",
    "npm",
    "yarn",
    "cargo",
    "go",
    "docker",
    "kubectl",
    "tt",
    "toolbelt",
    "brew",
    "pip",
    "pip3",
    "claude",
    "bun",
    "npx",
    "poetry",
    "terraform",
    "helm",
    "task",
    "make",
}

_PROGRAM_CATEGORIES: dict[str, str] = {
    "git": "vcs",
    "gh": "github",
    "pytest": "test",
    "vitest": "test",
    "jest": "test",
    "ruff": "lint",
    "mypy": "lint",
    "pyright": "lint",
    "eslint": "lint",
    "prettier": "lint",
    "tsc": "lint",
    "black": "lint",
    "make": "build",
    "cmake": "build",
    "rg": "search",
    "grep": "search",
    "ag": "search",
    "find": "search",
    "fd": "search",
    "cat": "read",
    "bat": "read",
    "sed": "read",
    "awk": "read",
    "head": "read",
    "tail": "read",
    "wc": "read",
    "jq": "read",
    "ls": "files",
    "tree": "files",
    "mkdir": "files",
    "cp": "files",
    "mv": "files",
    "rm": "files",
    "touch": "files",
    "chmod": "files",
    "ln": "files",
    "stat": "files",
    "du": "files",
    "pwd": "files",
    "which": "files",
    "curl": "network",
    "wget": "network",
    "http": "network",
    "ssh": "network",
    "nc": "network",
    "lsof": "network",
    "docker": "infra",
    "kubectl": "infra",
    "terraform": "infra",
    "helm": "infra",
    "launchctl": "infra",
    "tt": "toolbelt",
    "toolbelt": "toolbelt",
    "python": "run",
    "python3": "run",
    "node": "run",
    "bun": "run",
    "deno": "run",
    "ruby": "run",
    "sqlite3": "run",
}

_TEST_WORDS = {"test", "pytest", "vitest", "jest", "check:types"}
_BUILD_WORDS = {"build", "compile", "bundle", "dev", "start", "serve"}
_LINT_WORDS = {"lint", "typecheck", "format", "fmt", "check", "mypy", "ruff", "eslint"}
_DEPS_WORDS = {
    "install",
    "add",
    "sync",
    "remove",
    "update",
    "upgrade",
    "lock",
    "i",
    "ci",
}


@dataclass(frozen=True)
class Command:
    """What a Bash command is: its main program, that program's subcommand
    (``git`` ``status``), every program in its chain, and a category."""

    program: str | None
    subcommand: str | None
    programs: tuple[str, ...]
    category: str


def tokenize(command: str) -> list[list[str]]:
    """The commands of a chain, each as its words (quotes honoured): split on
    ``&&``, ``||``, ``;``, ``|``, ``&``, newlines and parentheses."""
    commands: list[list[str]] = []
    words: list[str] = []
    word = ""
    has_word = False
    quote: str | None = None

    def end_word() -> None:
        nonlocal word, has_word
        if has_word:
            words.append(word)
        word, has_word = "", False

    def end_command() -> None:
        nonlocal words
        end_word()
        if words:
            commands.append(words)
        words = []

    i = 0
    while i < len(command):
        c = command[i]
        if quote is not None:
            if c == quote:
                quote = None
            elif c == "\\" and quote == '"' and i + 1 < len(command):
                i += 1
                word += command[i]
            else:
                word += c
        elif c in "\"'":
            quote, has_word = c, True
        elif c == "\\" and i + 1 < len(command):
            i += 1
            word += command[i]
            has_word = True
        elif c in " \t":
            end_word()
        elif c in ";|&\n()":
            end_command()
        else:
            word += c
            has_word = True
        i += 1
    end_command()
    return commands


def _strip(words: list[str]) -> list[str]:
    """``words`` without leading ``VAR=value`` assignments and wrappers
    (``sudo``, ``time``, ``env`` ...)."""
    while words:
        head = words[0]
        if (
            "=" in head
            and head.split("=", 1)[0].replace("_", "a").isalnum()
            or head in _WRAPPERS
        ):
            words = words[1:]
        else:
            break
    return words


def _subcommand(program: str, words: list[str]) -> str | None:
    """The first word after ``program`` that is not an option or an option's value."""
    skip = False
    for word in words[1:]:
        if skip:
            skip = False
        elif word in _VALUE_OPTIONS:
            skip = True
        elif word.startswith("-"):
            continue
        else:
            return word
    return None


def _category(program: str, subcommand: str | None, words: list[str]) -> str:
    if program in ("uv", "pnpm", "npm", "yarn", "bun", "poetry", "npx", "task"):
        # ``uv run pytest``: what it runs decides; ``pnpm test``: the script.
        inner = subcommand
        if subcommand in ("run", "exec", "x", "tool", "dlx") and program != "task":
            rest = [
                w for w in words[words.index(subcommand) + 1 :] if not w.startswith("-")
            ]
            inner = rest[0] if rest else subcommand
            if inner in _PROGRAM_CATEGORIES and program == "uv":
                return _PROGRAM_CATEGORIES[inner]
        if inner in _TEST_WORDS or (inner or "").startswith("test"):
            return "test"
        if inner in _LINT_WORDS or (inner or "").startswith("lint"):
            return "lint"
        if inner in _BUILD_WORDS or (inner or "").startswith("build"):
            return "build"
        if subcommand in _DEPS_WORDS or program == "npx":
            return "deps" if subcommand in _DEPS_WORDS else "run"
        return "run"
    if program in ("cargo", "go"):
        if subcommand in ("test", "bench"):
            return "test"
        if subcommand in ("build", "run", "install", "check", "vet", "clippy", "fmt"):
            return {
                "build": "build",
                "run": "run",
                "install": "deps",
                "check": "lint",
                "vet": "lint",
                "clippy": "lint",
                "fmt": "lint",
            }[subcommand]
        return "build"
    if program in ("pip", "pip3", "brew"):
        return "deps"
    if program == "make":
        return (
            "test"
            if subcommand in _TEST_WORDS
            else "lint"
            if subcommand in _LINT_WORDS
            else "build"
        )
    if program == "claude":
        return "test" if subcommand == "plugin" and "test" in words else "toolbelt"
    if program in ("tt", "toolbelt") and subcommand in ("git", "task"):
        return "toolbelt"
    return _PROGRAM_CATEGORIES.get(program, "other")


def classify_bash(command: str) -> Command:
    """What ``command`` is. The main program is the first of the chain that is
    not navigation or a filter of what came before; an empty or purely
    navigational command has none and is ``other``."""
    segments = [_strip(words) for words in tokenize(command)]
    segments = [words for words in segments if words]
    programs = tuple(words[0] for words in segments)
    main: list[str] | None = None
    for words in segments:
        if words[0] in _NAVIGATION:
            continue
        if words[0] in _FILTERS and main is not None:
            continue
        main = words
        break
    if main is None:
        return Command(None, None, programs, "other")
    program = main[0].rsplit("/", 1)[-1]
    subcommand = _subcommand(program, main) if program in _WITH_SUBCOMMANDS else None
    return Command(program, subcommand, programs, _category(program, subcommand, main))


def classify(tool: str, detail: str) -> Command:
    """What a tool call is: a Bash command by its program, any other tool by
    what kind of tool it is."""
    if tool == "Bash":
        return classify_bash(detail)
    if tool.startswith("mcp__"):
        return Command(tool, None, (tool,), "mcp")
    return Command(tool, None, (tool,), TOOL_CATEGORIES.get(tool, "other"))

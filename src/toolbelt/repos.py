import os
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path

from toolbelt.env_var import get_git_projects_workdir


class AfterFileEditHook(ABC):
    def run(self, *, file_path: Path) -> None:
        pass


type Hook = AfterFileEditHook


class Repo(ABC):
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def unit_cmd(self) -> list[str]: ...

    def _run(self, cmd: list[str]) -> None:
        os.chdir(self.path())
        subprocess.run(cmd, check=True)

    def hooks(self) -> list[Hook]:
        return []

    def unit(self) -> None:
        self._run(self.unit_cmd())

    def path(self) -> Path:
        git_projects_workdir = os.getenv("GIT_PROJECTS_WORKDIR")
        if git_projects_workdir is None:
            raise ValueError("GIT_PROJECTS_WORKDIR environment variable is not set.")
        return Path(git_projects_workdir) / self.name()


class AiCodegeApi(Repo):
    def name(self) -> str:
        return "ai-codegen-api"

    def unit_cmd(self) -> list[str]:
        return ["task", "test"]

    def hooks(self) -> list[Hook]:
        repo_root = self.path()

        class AiCodegeApiAfterFileEditHook(AfterFileEditHook):
            def run(self, *, file_path: Path) -> None:
                subprocess.run(
                    ["uv", "run", "ruff", "format", str(file_path)],
                    cwd=repo_root,
                    check=True,
                )

        return [AiCodegeApiAfterFileEditHook()]


class DbtMcp(Repo):
    def name(self) -> str:
        return "dbt-mcp"

    def unit_cmd(self) -> list[str]:
        return ["task", "test"]


class MetricflowServer(Repo):
    def name(self) -> str:
        return "metricflow-server"

    def unit_cmd(self) -> list[str]:
        return ["make", "test"]


class Metricflow(Repo):
    def name(self) -> str:
        return "metricflow"

    def unit_cmd(self) -> list[str]:
        return ["make", "test"]


class DbtSemanticInterfaces(Repo):
    def name(self) -> str:
        return "dbt-semantic-interfaces"

    def unit_cmd(self) -> list[str]:
        return ["make", "test"]


repos: list[Repo] = [
    AiCodegeApi(),
    MetricflowServer(),
    Metricflow(),
    DbtSemanticInterfaces(),
]


def repo_for_path(file_path: Path) -> Repo | None:
    """
    Infer a known repo from an absolute file path, if possible.

    Matches against `GIT_PROJECTS_WORKDIR/<repo_name>/...`.
    """
    git_projects_workdir = os.getenv("GIT_PROJECTS_WORKDIR")
    if git_projects_workdir is None:
        return None

    try:
        relative = file_path.resolve().relative_to(Path(git_projects_workdir).resolve())
    except Exception:
        return None

    if not relative.parts:
        return None

    candidate_repo_name = relative.parts[0]
    for repo in repos:
        if repo.name() == candidate_repo_name:
            return repo
    return None


def current_repo_name() -> str:
    git_projects_workdir = get_git_projects_workdir()
    return Path(os.getcwd()).parts[: len(git_projects_workdir.parts) + 1][-1]


def current_repo() -> Repo | None:
    repo_name = current_repo_name()
    filtered_repos = list(filter(lambda r: r.name() == repo_name, repos))
    if not filtered_repos:
        return None
    if len(filtered_repos) != 1:
        raise ValueError("Unexpected result for repo search")
    return filtered_repos[0]

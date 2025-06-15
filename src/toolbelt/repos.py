from abc import ABC, abstractmethod
import os
from pathlib import Path
import subprocess
from toolbelt.env_var import get_git_projects_workdir


class Repo(ABC):
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def check_cmd(self) -> list[str]: ...

    def _run(self, cmd: list[str]) -> None:
        os.chdir(self.path())
        subprocess.run(cmd, check=True)

    def check(self) -> None:
        self._run(self.check_cmd())

    def path(self) -> Path:
        git_projects_workdir = os.getenv("GIT_PROJECTS_WORKDIR")
        if git_projects_workdir is None:
            raise ValueError("GIT_PROJECTS_WORKDIR environment variable is not set.")
        return Path(git_projects_workdir) / self.name()


class AiCodegeApi(Repo):
    def name(self) -> str:
        return "ai-codegen-api"

    def check_cmd(self) -> list[str]:
        return ["task", "test"]


class DbtMcp(Repo):
    def name(self) -> str:
        return "dbt-mcp"

    def check_cmd(self) -> list[str]:
        return ["task", "test"]


class MetricflowServer(Repo):
    def name(self) -> str:
        return "metricflow-server"

    def check_cmd(self) -> list[str]:
        return ["make", "test"]


class Metricflow(Repo):
    def name(self) -> str:
        return "metricflow"

    def check_cmd(self) -> list[str]:
        return ["make", "test"]


class DbtSemanticInterfaces(Repo):
    def name(self) -> str:
        return "dbt-semantic-interfaces"

    def check_cmd(self) -> list[str]:
        return ["make", "test"]


repos = [AiCodegeApi(), MetricflowServer(), Metricflow(), DbtSemanticInterfaces()]


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

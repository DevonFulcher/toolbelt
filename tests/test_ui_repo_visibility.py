from pathlib import Path

from toolbelt.git.stack.store import RepoRecord
from toolbelt.ui.repo_visibility import visible_repos


def repo(name: str, remote_url: str | None) -> RepoRecord:
    path = Path("/repos") / name
    return RepoRecord(
        id=hash(name) % 1000,
        name=name,
        path=path,
        git_common_dir=path / ".git",
        remote_url=remote_url,
    )


WORK = repo("work", "git@github.com:dbt-labs/work.git")
PERSONAL = repo("personal", "git@github.com:DevonFulcher/personal.git")
NO_REMOTE = repo("local", None)
NOT_GITHUB = repo("gitlab", "git@gitlab.com:someone/repo.git")
ALL = [WORK, PERSONAL, NO_REMOTE, NOT_GITHUB]
NO_CONFIG = Path("/nonexistent/config.yaml")


def test_a_personal_laptop_shows_every_repo() -> None:
    shown = visible_repos(ALL, env={"IS_PERSONAL": "true"}, config_file=NO_CONFIG)
    assert shown == ALL


def test_a_work_laptop_hides_repos_owned_outside_the_employer_orgs() -> None:
    shown = visible_repos(ALL, env={}, config_file=NO_CONFIG)
    assert shown == [WORK, NO_REMOTE, NOT_GITHUB]


def test_only_the_value_true_counts_as_personal() -> None:
    shown = visible_repos(ALL, env={"IS_PERSONAL": "1"}, config_file=NO_CONFIG)
    assert PERSONAL not in shown


def test_employer_orgs_come_from_the_config_file(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("tasks:\n  employer_orgs: [DevonFulcher]\n")
    shown = visible_repos(ALL, env={}, config_file=config)
    assert shown == [PERSONAL, NO_REMOTE, NOT_GITHUB]

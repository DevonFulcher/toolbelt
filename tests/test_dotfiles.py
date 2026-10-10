"""`tt dotfiles` against a throwaway HOME and two throwaway repos.

`home` is injected everywhere (core functions take it, the CLI takes a hidden
`--home`), so nothing here touches the real home directory.
"""

import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from toolbelt.dotfiles.actions import (
    BackupDir,
    backup_actions,
    link_actions,
    run_actions,
    unlink_actions,
)
from toolbelt.dotfiles.cli import dotfiles_typer
from toolbelt.dotfiles.config import config_path, dotfiles_repos
from toolbelt.dotfiles.manifest import expand_dest, load_manifest
from toolbelt.dotfiles.plan import build_plan
from toolbelt.dotfiles.secret_scan import scan_repo, scan_text
from toolbelt.dotfiles.status import State, collect_status

runner = CliRunner()

PUBLIC_MANIFEST = """
visibility = "public"
adopt_dir = "Mackup"

[[link]]
src = "Mackup/.zshrc"
dest = "~/.zshrc"

[[link]]
src = "Mackup/.config/tool"
dest = "~/.config/tool"
kind = "dir"

[[link]]
src = "Mackup/.claude/skills"
dest = "~/.claude/skills"
kind = "merge_dir"
"""

PRIVATE_MANIFEST = """
[[link]]
src = "skills"
dest = "~/.claude/skills"
kind = "merge_dir"

[[link]]
src = "home/.netrc"
dest = "~/.netrc"
"""

FIXED_NOW = datetime(2026, 1, 2, 3, 4, 5)


@dataclass
class Machine:
    home: Path
    public: Path
    private: Path
    config: Path

    def cli(self, *args: str):
        return runner.invoke(
            dotfiles_typer,
            ["--home", str(self.home), "--config", str(self.config), *args],
        )


def write(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def machine(tmp_path: Path) -> Machine:
    home = tmp_path / "home"
    public = tmp_path / "dotfiles"
    private = tmp_path / "dotfiles-private"
    home.mkdir()
    write(public / "dotfiles.toml", PUBLIC_MANIFEST)
    write(public / "Mackup/.zshrc", "export A=1\n")
    write(public / "Mackup/.config/tool/config", "c\n")
    write(public / "Mackup/.claude/skills/python/SKILL.md")
    write(public / "Mackup/.claude/skills/testing/SKILL.md")
    write(public / "Mackup/.claude/skills/.DS_Store")
    write(private / "dotfiles.toml", PRIVATE_MANIFEST)
    write(private / "skills/read-email/SKILL.md")
    write(private / "home/.netrc", "machine example\n")
    config = write(
        home / ".config/toolbelt/config.yaml",
        f"dotfiles:\n  repos:\n    - {public}\n    - {private}\n",
    )
    return Machine(home=home, public=public, private=private, config=config)


def plan_for(machine: Machine):
    manifests = [load_manifest(machine.public), load_manifest(machine.private)]
    return build_plan(manifests, home=machine.home)


def states(machine: Machine) -> dict[str, State]:
    return {
        str(entry.dest.relative_to(machine.home)): entry.state
        for entry in collect_status(plan_for(machine))
    }


def link_for_real(machine: Machine, now: datetime = FIXED_NOW) -> list[str]:
    plan = plan_for(machine)
    return run_actions(
        link_actions(plan), backups=BackupDir(machine.home, lambda: now), dry_run=False
    )


# --- config -----------------------------------------------------------------


def test_config_path_honors_xdg_config_home(tmp_path: Path) -> None:
    home = tmp_path / "h"
    assert config_path({}, home=home) == home / ".config/toolbelt/config.yaml"
    xdg = {"XDG_CONFIG_HOME": str(tmp_path / "xdg")}
    assert config_path(xdg, home=home) == tmp_path / "xdg/toolbelt/config.yaml"


def test_repos_default_and_configured(tmp_path: Path) -> None:
    home = tmp_path / "h"
    missing = tmp_path / "nope.yaml"
    assert dotfiles_repos(missing, home=home) == [
        home / "git/dotfiles",
        home / "git/dotfiles-private",
    ]
    config = write(tmp_path / "c.yaml", "sync:\n  repos: []\n")
    assert dotfiles_repos(config, home=home)[0] == home / "git/dotfiles"
    config = write(tmp_path / "c.yaml", "dotfiles:\n  repos: ['~/a', /abs/b]\n")
    assert dotfiles_repos(config, home=home) == [home / "a", Path("/abs/b")]


def test_relative_repo_entry_is_rejected(tmp_path: Path) -> None:
    config = write(tmp_path / "c.yaml", "dotfiles:\n  repos: [relative]\n")
    with pytest.raises(ValueError, match="relative"):
        dotfiles_repos(config, home=tmp_path)


# --- manifest and plan ------------------------------------------------------


def test_expand_dest(tmp_path: Path) -> None:
    assert expand_dest("~/.zshrc", home=tmp_path) == tmp_path / ".zshrc"
    assert expand_dest("/etc/x", home=tmp_path) == Path("/etc/x")
    with pytest.raises(ValueError):
        expand_dest("relative/x", home=tmp_path)


def test_unknown_manifest_key_is_rejected(tmp_path: Path) -> None:
    write(tmp_path / "dotfiles.toml", '[[link]]\nsrc="a"\ndest="~/a"\nkidn="file"\n')
    with pytest.raises(ValueError):
        load_manifest(tmp_path)


def test_merge_dir_combines_repos_and_skips_hidden(machine: Machine) -> None:
    plan = plan_for(machine)
    skills = sorted(t.dest.name for t in plan.targets if t.dest.parent.name == "skills")
    assert skills == ["python", "read-email", "testing"]
    assert plan.containers == [machine.home / ".claude/skills"]


def test_later_repo_overrides_same_destination(machine: Machine) -> None:
    write(machine.private / "skills/python/SKILL.md", "private override")
    plan = plan_for(machine)
    python = next(t for t in plan.targets if t.dest.name == "python")
    assert python.src == machine.private / "skills/python"


# --- status -----------------------------------------------------------------


def test_status_on_fresh_home_is_all_missing(machine: Machine) -> None:
    result = machine.cli("status")
    assert result.exit_code == 1
    assert states(machine)[".zshrc"] is State.MISSING
    assert "missing" in result.output


def test_status_classifies_each_state(machine: Machine) -> None:
    home = machine.home
    (home / ".zshrc").symlink_to(machine.public / "Mackup/.zshrc")
    write(home / ".netrc", "real file")
    (home / ".config").mkdir(exist_ok=True)
    (home / ".config/tool").symlink_to(machine.private)
    seen = states(machine)
    assert seen[".zshrc"] is State.OK
    assert seen[".netrc"] is State.CONFLICT
    assert seen[".config/tool"] is State.WRONG_TARGET
    (machine.public / "Mackup/.config/tool").rename(machine.public / "Mackup/moved")
    assert states(machine)[".config/tool"] is State.MISSING_SOURCE


def test_status_accepts_trailing_slash_and_relative_links(machine: Machine) -> None:
    skills = machine.home / ".claude/skills"
    skills.mkdir(parents=True)
    src = machine.public / "Mackup/.claude/skills/python"
    (skills / "python").symlink_to(f"{src}/")
    (skills / "testing").symlink_to(
        os.path.relpath(machine.public / "Mackup/.claude/skills/testing", skills)
    )
    seen = states(machine)
    assert seen[".claude/skills/python"] is State.OK
    assert seen[".claude/skills/testing"] is State.OK


def test_status_reports_stale_link_into_repo(machine: Machine) -> None:
    skills = machine.home / ".claude/skills"
    skills.mkdir(parents=True)
    (skills / "gone").symlink_to(machine.public / "Mackup/.claude/skills/gone")
    (skills / "elsewhere").symlink_to(machine.home / "nowhere")
    seen = states(machine)
    assert seen[".claude/skills/gone"] is State.STALE
    assert ".claude/skills/elsewhere" not in seen


# --- link -------------------------------------------------------------------


def test_link_creates_everything_and_is_idempotent(machine: Machine) -> None:
    link_for_real(machine)
    assert set(states(machine).values()) == {State.OK}
    assert (machine.home / ".zshrc").read_text() == "export A=1\n"
    assert (machine.home / ".claude/skills").is_dir()
    assert not (machine.home / ".claude/skills").is_symlink()
    assert (machine.home / ".claude/skills/read-email").is_symlink()
    assert not (machine.home / ".toolbelt").exists()
    assert link_actions(plan_for(machine)) == []


def test_link_backs_up_real_file_before_replacing(machine: Machine) -> None:
    write(machine.home / ".zshrc", "my precious local zshrc")
    lines = link_for_real(machine)
    backup = machine.home / ".toolbelt/dotfiles-backups/20260102T030405/.zshrc"
    assert backup.read_text() == "my precious local zshrc"
    assert (machine.home / ".zshrc").is_symlink()
    assert any(line.startswith("backup") for line in lines)


def test_link_backs_up_wrong_target_symlink_and_real_dir(machine: Machine) -> None:
    (machine.home / ".zshrc").symlink_to("/nonexistent")
    write(machine.home / ".config/tool/mine", "local")
    link_for_real(machine)
    root = machine.home / ".toolbelt/dotfiles-backups/20260102T030405"
    assert (root / ".zshrc").is_symlink()
    assert (root / ".config/tool/mine").read_text() == "local"
    assert set(states(machine).values()) == {State.OK}


def test_link_replaces_symlinked_skills_dir_without_touching_repo(
    machine: Machine,
) -> None:
    skills = machine.home / ".claude/skills"
    skills.parent.mkdir(parents=True)
    skills.symlink_to(machine.public / "Mackup/.claude/skills")
    link_for_real(machine)
    assert not skills.is_symlink()
    assert (skills / "read-email").is_symlink()
    # The repo's skills directory must not have gained the private skill.
    assert not (machine.public / "Mackup/.claude/skills/read-email").exists()
    assert (
        machine.home / ".toolbelt/dotfiles-backups/20260102T030405/.claude/skills"
    ).is_symlink()


def test_link_removes_stale_links(machine: Machine) -> None:
    link_for_real(machine)
    skills = machine.home / ".claude/skills"
    (skills / "gone").symlink_to(machine.public / "Mackup/.claude/skills/gone")
    link_for_real(machine)
    assert not (skills / "gone").is_symlink()


def test_dry_run_changes_nothing(machine: Machine) -> None:
    write(machine.home / ".zshrc", "local")
    before = sorted(p for p in machine.home.rglob("*"))
    result = machine.cli("link", "--dry-run")
    assert result.exit_code == 0, result.output
    assert "[dry-run] backup" in result.output
    assert "[dry-run] link" in result.output
    assert sorted(machine.home.rglob("*")) == before
    assert (machine.home / ".zshrc").read_text() == "local"
    assert not (machine.home / ".toolbelt").exists()


def test_link_cli_end_to_end(machine: Machine) -> None:
    result = machine.cli("link")
    assert result.exit_code == 0, result.output
    assert machine.cli("status").exit_code == 0
    assert "Nothing to do." in machine.cli("link").output


def test_copy_mode(machine: Machine) -> None:
    write(
        machine.public / "dotfiles.toml",
        PUBLIC_MANIFEST
        + '\n[[link]]\nsrc="Mackup/.zshrc"\ndest="~/.zshrc"\nmode="copy"\n',
    )
    link_for_real(machine)
    zshrc = machine.home / ".zshrc"
    assert zshrc.is_file() and not zshrc.is_symlink()
    assert states(machine)[".zshrc"] is State.OK
    zshrc.write_text("edited")
    assert states(machine)[".zshrc"] is State.CONFLICT
    link_for_real(machine, now=datetime(2026, 1, 2, 3, 4, 6))
    backup = machine.home / ".toolbelt/dotfiles-backups/20260102T030406/.zshrc"
    assert backup.read_text() == "edited"
    assert zshrc.read_text() == "export A=1\n"


def test_missing_source_aborts_link(machine: Machine) -> None:
    (machine.public / "Mackup/.zshrc").unlink()
    with pytest.raises(FileNotFoundError):
        link_actions(plan_for(machine))


# --- unlink / backup --------------------------------------------------------


def test_unlink_removes_only_links_into_repo(machine: Machine) -> None:
    link_for_real(machine)
    write(machine.home / ".claude/skills/own-skill/SKILL.md")
    (machine.home / ".netrc").unlink()
    write(machine.home / ".netrc", "real")
    run_actions(
        unlink_actions(plan_for(machine)),
        backups=BackupDir(machine.home, lambda: FIXED_NOW),
        dry_run=False,
    )
    assert not (machine.home / ".zshrc").exists()
    assert not (machine.home / ".claude/skills/python").exists()
    assert (machine.home / ".claude/skills/own-skill/SKILL.md").exists()
    assert (machine.home / ".netrc").read_text() == "real"
    # The repo is untouched.
    assert (machine.public / "Mackup/.zshrc").read_text() == "export A=1\n"


def test_backup_command_copies_without_changing(machine: Machine) -> None:
    write(machine.home / ".zshrc", "local")
    run_actions(
        backup_actions(plan_for(machine)),
        backups=BackupDir(machine.home, lambda: FIXED_NOW),
        dry_run=False,
    )
    root = machine.home / ".toolbelt/dotfiles-backups/20260102T030405"
    assert (root / ".zshrc").read_text() == "local"
    assert (machine.home / ".zshrc").read_text() == "local"


def test_backup_dir_does_not_collide(machine: Machine) -> None:
    first = BackupDir(machine.home, lambda: FIXED_NOW)
    first.root.mkdir(parents=True)
    second = BackupDir(machine.home, lambda: FIXED_NOW)
    assert second.root != first.root


# --- adopt ------------------------------------------------------------------


def test_adopt_moves_file_into_private_repo_and_links(machine: Machine) -> None:
    write(machine.home / ".config/new/app.conf", "setting=1\n")
    result = machine.cli("adopt", str(machine.home / ".config/new/app.conf"))
    assert result.exit_code == 0, result.output
    stored = machine.private / "home/.config/new/app.conf"
    assert stored.read_text() == "setting=1\n"
    assert (machine.home / ".config/new/app.conf").is_symlink()
    assert '"~/.config/new/app.conf"' in (machine.private / "dotfiles.toml").read_text()
    assert machine.cli("status").exit_code == 1  # other links still missing
    assert states(machine)[".config/new/app.conf"] is State.OK


def test_adopt_dry_run_changes_nothing(machine: Machine) -> None:
    target = write(machine.home / ".thing", "v")
    manifest = (machine.private / "dotfiles.toml").read_text()
    result = machine.cli("adopt", str(target), "--dry-run")
    assert result.exit_code == 0, result.output
    assert not target.is_symlink()
    assert (machine.private / "dotfiles.toml").read_text() == manifest
    assert not (machine.private / "home/.thing").exists()


def test_adopt_into_public_repo_refuses_secrets(machine: Machine) -> None:
    key = "ghp_" + "a" * 36
    target = write(machine.home / ".tokenrc", f"token {key}\n")
    result = machine.cli("adopt", str(target), "--repo", "dotfiles")
    assert result.exit_code != 0
    assert target.read_text() == f"token {key}\n"
    assert not target.is_symlink()
    assert key not in result.output


def test_adopt_into_public_repo_uses_adopt_dir(machine: Machine) -> None:
    target = write(machine.home / ".harmless", "ok\n")
    result = machine.cli("adopt", str(target), "--repo", "dotfiles")
    assert result.exit_code == 0, result.output
    assert (machine.public / "Mackup/.harmless").read_text() == "ok\n"


def test_adopt_rejects_symlinks_and_managed_paths(machine: Machine) -> None:
    link_for_real(machine)
    assert machine.cli("adopt", str(machine.home / ".zshrc")).exit_code != 0
    outside = write(machine.home.parent / "outside", "x")
    assert machine.cli("adopt", str(outside)).exit_code != 0


# --- check ------------------------------------------------------------------


def test_scan_text_flags_secrets_without_echoing_them() -> None:
    samples = {
        "AWS access key id": "AKIA" + "ABCDEFGHIJKLMNOP",
        "GitHub token": "ghp_" + "b" * 36,
        "private key block": "-----BEGIN OPENSSH PRIVATE KEY-----",
        "secret assignment": 'export API_TOKEN="' + "Zx9" * 8 + '"',
        "credential in URL": "https://user:hunter2hunter@example.com/x",
    }
    for rule, line in samples.items():
        findings = scan_text(line, path=Path("f"))
        assert rule in {f.rule for f in findings}
        assert all(line not in str(f) for f in findings)


def test_scan_text_ignores_references_and_allow_marker() -> None:
    assert scan_text('export TOKEN="$(op read op://v/i/t)"', path=Path("f")) == []
    assert scan_text("export TOKEN=${GITHUB_TOKEN}", path=Path("f")) == []
    assert scan_text("password = <your-password-here-xxxx>", path=Path("f")) == []
    allowed = "ghp_" + "c" * 36 + "  # tt:allow-secret"
    assert scan_text(allowed, path=Path("f")) == []


def test_check_scans_tracked_and_untracked_but_not_ignored(machine: Machine) -> None:
    repo = machine.public
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    write(repo / ".gitignore", "secrets\n")
    write(repo / "secrets/token", "ghp_" + "d" * 36)
    assert scan_repo(repo) == []
    write(repo / "Mackup/.zshrc", "export GH=" + "ghp_" + "e" * 36 + "\n")
    findings = scan_repo(repo)
    assert [(f.path.name, f.line, f.rule) for f in findings] == [
        (".zshrc", 1, "GitHub token")
    ]
    result = machine.cli("check")
    assert result.exit_code == 1
    assert "ghp_" not in result.output


def test_check_passes_for_clean_public_repo(machine: Machine) -> None:
    subprocess.run(["git", "init", "-q"], cwd=machine.public, check=True)
    result = machine.cli("check")
    assert result.exit_code == 0, result.output

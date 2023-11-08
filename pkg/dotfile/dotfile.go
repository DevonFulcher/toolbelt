package dotfile

import (
	"path"
	"toolbelt/internal/config"
	"toolbelt/pkg/fs"
	"toolbelt/pkg/git"
	"toolbelt/pkg/shell"
	"toolbelt/pkg/vscode"
)

func Pull() error {
	err := git.CloneIfNotExist(config.REPOS_PATH, config.GITHUB_USERNAME, config.DOTFILES_REPO)
	if err != nil {
		return err
	}

	dotfiles := path.Join(config.REPOS_PATH, config.DOTFILES_REPO)
	c := shell.NewWithDir(dotfiles, "git pull")
	_, err = c.RunCmd()
	if err != nil {
		return err
	}

	err = fs.CopyFile(config.VSCODE_DOTFILES_SETTINGS, config.VSCODE_USER_SETTINGS)
	if err != nil {
		return err
	}

	return vscode.PullExtensions()
}

func Push() error {
	err := git.CloneIfNotExist(config.REPOS_PATH, config.GITHUB_USERNAME, config.DOTFILES_REPO)
	if err != nil {
		return err
	}

	err = fs.CopyFile(config.VSCODE_USER_SETTINGS, config.VSCODE_DOTFILES_SETTINGS)
	if err != nil {
		return err
	}

	return git.GitSave(config.DOTFILES_PATH, "dot files push")
}

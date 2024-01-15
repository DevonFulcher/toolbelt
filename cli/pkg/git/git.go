package git

import (
	"fmt"
	"os"
	"path"
	"toolbelt/internal/config"
	"toolbelt/pkg/shell"
)

func PullRepos() error {
	dirs, err := os.ReadDir(config.REPOS_PATH)
	if err != nil {
		return err
	}
	cmds := []shell.Cmd{}
	for _, dir := range dirs {
		repoPath := path.Join(config.REPOS_PATH, dir.Name())
		cmds = append(cmds, shell.NewWithDir(repoPath, "git pull"))
	}
	err = shell.RunCmdsConcurrent(cmds)
	if err != nil {
		return err
	}
	return nil
}

func CloneIfNotExist(parentDirPath string, org string, repo string) error {
	repoCloneArg := fmt.Sprintf("git@github.com:%v/%v.git", org, repo)
	repoPath := path.Join(parentDirPath, repo)
	if _, err := os.Stat(repoPath); os.IsNotExist(err) {
		c := shell.NewWithDir(parentDirPath, "git clone %v", repoCloneArg)
		_, err := c.RunCmd()
		if err != nil {
			return err
		}
	}
	return nil
}

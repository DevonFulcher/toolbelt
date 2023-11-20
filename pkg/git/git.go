package git

import (
	"fmt"
	"os"
	"path"
	"strconv"
	"toolbelt/internal/config"
	"toolbelt/pkg/shell"
)

func GitSave(dir string, message string) error {
	cmds := []shell.Cmd{}
	cmds = append(cmds, []shell.Cmd{
		shell.NewWithDir(dir, "git add -A"),
		shell.NewFromArrayWithDir(dir, []string{"git", "commit", "-m", message}),
		shell.NewWithDir(dir, "git push"),
	}...)
	_, err := shell.RunCmds(cmds)
	if err != nil {
		return err
	}
	return nil
}

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

func Sync() error {
	cmd := shell.New("git add -A")
	_, err := cmd.RunCmd()
	if err != nil {
		return err
	}
	cmd = shell.New("git diff --cached --numstat | wc -l")
	stdout, err := cmd.RunCmd()
	if err != nil {
		return err
	}
	numStashedFiles, err := strconv.Atoi(stdout)
	if err != nil {
		return err
	}
	cmds := []shell.Cmd{
		shell.New("git stash"),
		shell.New("git checkout %v", config.DEFAULT_BRANCH),
		shell.New("git pull"),
		shell.New("git checkout -"),
		shell.New("git merge %v", config.DEFAULT_BRANCH),
	}
	_, err = shell.RunCmds(cmds)
	if err != nil {
		return err
	}
	if numStashedFiles > 0 {
		cmd = shell.New("git stash pop")
		_, err = cmd.RunCmd()
		if err != nil {
			return err
		}
	}
	return nil
}

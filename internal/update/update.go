package update

import (
	"path"
	"toolbelt/internal/config"
	"toolbelt/pkg/shell"
)

func Run() error {
	dir := path.Join(config.REPOS_PATH, config.REPO_NAME)
	cmds := []shell.Cmd{
		shell.NewWithDir(dir, "git pull"),
		shell.NewWithDir(dir, "go build"),
		shell.NewWithDir(dir, "cp %v %v", config.EXECUTABLE_NAME, config.CLI_PATH),
	}
	_, err := shell.RunCmds(cmds)
	if err != nil {
		return err
	}
	return nil
}

package morning

import (
	"toolbelt/pkg/git"
	"toolbelt/pkg/shell"
)

func Run() error {
	c := shell.New("aws sso login")
	_, err := c.RunCmd()
	if err != nil {
		return err
	}
	return git.PullRepos()
}

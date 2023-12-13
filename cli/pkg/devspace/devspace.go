package devspace

import (
	"os"
	"toolbelt/internal/config"
	"toolbelt/pkg/shell"
)

func Reset() error {
	err := os.Remove("~/.devspace")
	if err != nil {
		return err
	}
	cmds := []shell.Cmd{
		shell.New("fsh dev destroy %v", config.DEVSPACE_NAMESPACE),
		shell.New("devspace use namespace %v", config.DEVSPACE_NAMESPACE),
	}
	_, err = shell.RunCmds(cmds)
	if err != nil {
		return err
	}
	return nil
}

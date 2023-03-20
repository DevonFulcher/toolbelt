package cmd

import (
	"fmt"
	"os"
	"os/exec"
	"strings"
)

type Cmd struct {
	dir *string
	cmd []string
}

func New(fmt string, vars ...string) Cmd {
	for _, curr := range vars {
		fmt = strings.Replace(fmt, "%v", curr, 1)
	}
	cmdSplit := strings.Split(fmt, " ")
	return Cmd{nil, cmdSplit}
}

func NewCmdFromArray(cmd []string) Cmd {
	return Cmd{nil, cmd}
}

func NewCmds(cmds ...string) []Cmd {
	result := []Cmd{}
	for _, cmd := range cmds {
		result = append(result, New(cmd))
	}
	return result
}

func (c *Cmd) RunCmd() error {
	fmt.Println(c)
	toRun := exec.Command(c.cmd[0], c.cmd[1:]...)
	toRun.Stdout = os.Stdout
	toRun.Stdin = os.Stdin
	if c.dir != nil {
		toRun.Dir = *c.dir
	}
	if err := toRun.Run(); err != nil {
		return fmt.Errorf("could not run command: %v with error: %v", c, err)
	}
	return nil
}

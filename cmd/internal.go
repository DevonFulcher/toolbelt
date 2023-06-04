package cmd

import (
	"fmt"
	"os"
	"os/exec"
	"strings"
)

type Internal struct {
	dir *string
	cmd []string
}

func New(cmd string, vars ...string) Internal {
	return Internal{nil, createCmdArrary(cmd, vars)}
}

func NewWithDir(dir, cmd string, vars ...string) Internal {
	return Internal{&dir, createCmdArrary(cmd, vars)}
}

func NewFromArray(cmd []string) Internal {
	return Internal{nil, cmd}
}

func createCmdArrary(cmd string, vars []string) []string {
	for _, curr := range vars {
		cmd = strings.Replace(cmd, "%v", curr, 1)
	}
	return strings.Split(cmd, " ")
}

func NewCmds(cmds ...string) []Internal {
	result := []Internal{}
	for _, cmd := range cmds {
		result = append(result, New(cmd))
	}
	return result
}

func (c *Internal) RunCmd() error {
	if c.dir != nil {
		fmt.Printf("dir: %v cmd: %v\n", *c.dir, c.cmd)
	} else {
		fmt.Printf("cmd %v\n", c.cmd)
	}
	toRun := exec.Command(c.cmd[0], c.cmd[1:]...)
	toRun.Stdout = os.Stdout
	toRun.Stdin = os.Stdin
	toRun.Stderr = os.Stderr
	if c.dir != nil {
		toRun.Dir = *c.dir
	}
	if err := toRun.Run(); err != nil {
		return fmt.Errorf("could not run command: %v with error: %v", c.cmd, err)
	}
	return nil
}

func RunCmds(cmds []Internal) error {
	for _, cmd := range cmds {
		err := cmd.RunCmd()
		if err != nil {
			return err
		}
	}
	return nil
}

func PrintCmds(cmds [][]string) {
	for _, cmd := range cmds {
		fmt.Println()
		fmt.Println(cmd[0])
		fmt.Printf("- %v", cmd[1])
		fmt.Println()
	}
}

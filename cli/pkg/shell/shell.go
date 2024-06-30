package shell

import (
	"bytes"
	"fmt"
	"os/exec"
	"strings"
)

type Cmd struct {
	dir *string
	cmd []string
}

func New(cmd string, vars ...string) Cmd {
	return Cmd{nil, createCmdArrary(cmd, vars)}
}

func NewWithDir(dir, cmd string, vars ...string) Cmd {
	return Cmd{&dir, createCmdArrary(cmd, vars)}
}

func NewFromArray(cmd []string) Cmd {
	return Cmd{nil, cmd}
}

func NewFromArrayWithDir(dir string, cmd []string) Cmd {
	return Cmd{&dir, cmd}
}

func createCmdArrary(cmd string, vars []string) []string {
	for _, curr := range vars {
		cmd = strings.Replace(cmd, "%v", curr, 1)
	}
	return strings.Split(cmd, " ")
}

func NewCmds(cmds ...string) []Cmd {
	result := []Cmd{}
	for _, cmd := range cmds {
		result = append(result, New(cmd))
	}
	return result
}

func (c *Cmd) RunCmd() (string, error) {
	if c.dir != nil {
		fmt.Printf("dir: %v cmd: %v\n", *c.dir, c.cmd)
	} else {
		fmt.Printf("cmd: %v\n", c.cmd)
	}
	toRun := exec.Command(c.cmd[0], c.cmd[1:]...)
	var stdout, stderr bytes.Buffer
	toRun.Stdout = &stdout
	toRun.Stderr = &stderr
	// toRun.Stdin = os.Stdin
	if c.dir != nil {
		toRun.Dir = *c.dir
	}
	if err := toRun.Run(); err != nil {
		return "", fmt.Errorf("could not run command: %v\n in dir %v\n with error message: %v\n and stderr: %v", c.cmd, *c.dir, err, stderr.String())
	}
	printOut := stdout.String()
	if printOut != "" {
		fmt.Println(printOut)
	}
	return printOut, nil
}

func RunCmdsFromStr(cmds ...string) ([]string, error) {
	result := []Cmd{}
	for _, cmd := range cmds {
		result = append(result, New(cmd))
	}
	return RunCmds(result)
}

func RunCmds(cmds []Cmd) ([]string, error) {
	outs := []string{}
	for _, cmd := range cmds {
		out, err := cmd.RunCmd()
		if err != nil {
			return nil, err
		}
		outs = append(outs, out)
	}
	return outs, nil
}

func PrintCmds(cmds [][]string) {
	for _, cmd := range cmds {
		fmt.Println()
		fmt.Println(cmd[0])
		fmt.Printf("- %v", cmd[1])
		fmt.Println()
	}
}

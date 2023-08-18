package cmd

import (
	"bytes"
	"fmt"
	"os"
	"os/exec"
	"strings"
	"sync"
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

func NewFromArrayWithDir(dir string, cmd []string) Internal {
	return Internal{&dir, cmd}
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

func (c *Internal) RunCmd() (string, error) {
	if c.dir != nil {
		fmt.Printf("dir: %v cmd: %v\n", *c.dir, c.cmd)
	} else {
		fmt.Printf("cmd %v\n", c.cmd)
	}
	toRun := exec.Command(c.cmd[0], c.cmd[1:]...)
	var stdout bytes.Buffer
	var stderr bytes.Buffer
	toRun.Stdout = &stdout
	toRun.Stdin = os.Stdin
	toRun.Stderr = &stderr
	if c.dir != nil {
		toRun.Dir = *c.dir
	}
	if err := toRun.Run(); err != nil {
		return "", fmt.Errorf("could not run command: %v\n in dir %v\n with error message: %v\n and stderr: %v", c.cmd, *c.dir, err, toRun.Stderr)
	}
	printOut := stdout.String()
	if printOut != "" {
		fmt.Println(printOut)
	}
	return printOut, nil
}

func RunCmds(cmds []Internal) ([]string, error) {
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

func RunCmdsConcurrent(cmds []Internal) error {
	errs := []string{}
	errCmds := []string{}
	var wg sync.WaitGroup
	for _, cmd := range cmds {
		wg.Add(1)
		go func(c Internal) {
			defer wg.Done()
			_, err := c.RunCmd()
			if err != nil {
				errs = append(errs, err.Error())
				cmdString := strings.Join(c.cmd, " ")
				errCmds = append(errCmds, fmt.Sprintf("cmd: %v dir: %v", cmdString, c.dir))
			}
		}(cmd)
	}
	wg.Wait()
	if len(errs) > 0 {
		return fmt.Errorf("errors: %v\nerror commands: %v", strings.Join(errs, "\n"), strings.Join(errCmds, "\n"))
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

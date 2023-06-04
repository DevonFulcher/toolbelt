package cmd

import (
	"errors"
	"fmt"
	"os"
	"strings"
	"toolbelt/config"
)

type External struct {
	name        string
	description string
	children    []External
	run         func(params []string) error
}

var CmdTree = []External{
	{
		name:        "git",
		description: "git utilities",
		children: []External{
			{
				name:        "save",
				description: "save progress and push it to remote",
				run: func(param []string) error {
					cmds := []Internal{}
					path, _ := os.Getwd()
					if strings.Contains(path, config.SLG_REPO) {
						cmds = append(cmds, New("gradle ktlintFormat"))
					}
					cmds = append(cmds, []Internal{
						New("git add -A"),
						NewFromArray([]string{"git", "commit", "-m", param[0]}),
						New("git push"),
					}...)
					return RunCmds(cmds)
				},
			},
		},
	},
}

func findCmd(input string, cmds []External) (*External, error) {
	for _, cmd := range cmds {
		if input == cmd.name {
			return &cmd, nil
		}
	}
	return nil, fmt.Errorf("Invalid input. %v is not valid", input)
}

func findRoot(input []string) (*External, error) {
	if len(input) < 1 {
		return nil, errors.New("No input values")
	}
	first := input[0]
	return findCmd(first, CmdTree)
}

func Run(input []string) error {
	curr := CmdTree
	var cmd *External
	var err error
	i := 0
	for _, val := range input {
		cmd, err = findCmd(val, curr)
		i += 1
		if err != nil {
			return err
		}
		if cmd != nil && len(cmd.children) > 0 {
			curr = cmd.children
		} else {
			break
		}
	}
	return cmd.run(input[i:])
}

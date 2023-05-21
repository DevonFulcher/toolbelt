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
	run         func(params string) error
}

var CmdTree = []External{
	{
		name:        "git",
		description: "git utilities",
		children: []External{
			{
				name:        "save",
				description: "save progress and push it to remote",
				run: func(param string) error {
					cmds := []Internal{}
					path, _ := os.Getwd()
					if strings.Contains(path, config.SLG_REPO) {
						cmds = append(cmds, New("gradle ktlintFormat"))
					}
					cmds = append(cmds, []Internal{
						New("git add -A"),
						NewFromArray([]string{"git", "commit", "-m", param}),
						New("git push"),
					}...)
					return RunCmds(cmds)
				},
			},
		},
	},
}

func findCmd(input string, cmd []External) (*External, error) {
	for _, cmd := range cmd {
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

func Run(input []string) {
	// TODO: handle errors
	curr, _ := findRoot(input)
	for _, val := range input {
		if val == curr.name {
			if curr != nil && len(curr.children) > 0 {
				curr, _ = findCmd(val, curr.children)
			} else {
				_ = curr.run(val)
			}
		}
	}
}

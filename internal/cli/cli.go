package cli

import (
	"fmt"
	"toolbelt/internal/config"
	"toolbelt/internal/update"
	"toolbelt/pkg/devspace"
	"toolbelt/pkg/dotfile"
	"toolbelt/pkg/git"
	"toolbelt/pkg/kill"
	"toolbelt/pkg/morning"
	"toolbelt/pkg/repo"
)

type Command struct {
	name        string
	description string
	children    []Command
	run         func(params []string) error
}

var CmdTree = []Command{
	{
		name:        "git",
		description: "git utilities",
		children: []Command{
			{
				name:        "sync",
				description: "sync changes from main into branch",
				run: func(params []string) error {
					return git.Sync()
				},
			},
			{
				name:        "pull",
				description: "pull all repos in the repos folder",
				run: func(params []string) error {
					return git.PullRepos()
				},
			},
		},
	},
	{
		name:        "morning",
		description: "morning script",
		run: func(params []string) error {
			return morning.Run()
		},
	},
	{
		name:        "update",
		description: "update toolbelt",
		run: func(params []string) error {
			return update.Run()
		},
	},
	{
		name:        "kill",
		description: "kill a process for a given port",
		run: func(params []string) error {
			return kill.Port(params)
		},
	},
	{
		name:        "devspace",
		description: "utilities for devspace",
		children: []Command{
			{
				name:        "reset",
				description: "reset devspace",
				run: func(params []string) error {
					return devspace.Reset()
				},
			},
		},
	},
	{
		name:        "dot",
		description: "utilities for dotfiles",
		children: []Command{
			{
				name:        "pull",
				description: "pull in dotfile changes",
				run: func(params []string) error {
					return dotfile.Pull()
				},
			},
			{
				name:        "push",
				description: "push dotfile changes",
				run: func(params []string) error {
					return dotfile.Push()
				},
			},
			{
				name:        "list",
				description: "list dot files",
				run: func(params []string) error {
					fmt.Printf("vscode: %v\n", config.VSCODE_USER_SETTINGS)
					return nil
				},
			},
		},
	},
	{
		name:        "dev",
		description: "generic development utilities",
		children: []Command{
			{
				name:        "test",
				description: "run the tests",
				run: func(params []string) error {
					return repo.Current().Test()
				},
			},
			{
				name:        "run",
				description: "run the app locally",
				run: func(params []string) error {
					return repo.Current().Run()
				},
			},
			{
				name:        "lint",
				description: "run the lint checks",
				run: func(params []string) error {
					return repo.Current().Lint()
				},
			},
			{
				name:        "format",
				description: "format the repo",
				run: func(params []string) error {
					return repo.Current().Format()
				},
			},
		},
	},
}

func findCmd(input string, cmds []Command) (*Command, error) {
	for _, cmd := range cmds {
		if input == cmd.name {
			return &cmd, nil
		}
	}
	return nil, fmt.Errorf("invalid input. %v is not valid", input)
}

func printDescription(cmds []Command) {
	for _, cmd := range cmds {
		line := fmt.Sprintf("%v: %v", cmd.name, cmd.description)
		fmt.Println(line)
	}
}

func Run(input []string) error {
	if len(input) == 0 {
		printDescription(CmdTree)
		return nil
	}
	curr := CmdTree
	var cmd *Command
	var err error
	i := 0
	for _, val := range input {
		cmd, err = findCmd(val, curr)
		i += 1
		if err != nil {
			return err
		}
		if cmd == nil || cmd.children == nil || len(cmd.children) == 0 {
			break
		}
		curr = cmd.children
	}
	if cmd.run == nil {
		printDescription(cmd.children)
		return nil
	}
	return cmd.run(input[i:])
}

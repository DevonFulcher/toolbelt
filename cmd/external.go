package cmd

import (
	"errors"
	"fmt"
	"os"
	"path"
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
			{
				name:        "log",
				description: "pretty log git branches",
				run: func(params []string) error {
					arr := []string{"git", "log", "--graph", "--all", "--pretty='format:%C(auto)%h %C(cyan)%ar %C(auto)%d %C(magenta)%an %C(auto)%s'"}
					c := NewFromArray(arr)
					return c.RunCmd()
				},
			},
			{
				name:        "sync",
				description: "sync changes from main into branch",
				run: func(params []string) error {
					cmds := []Internal{
						New("git add -A"),
						New("git stash"),
						New("git checkout %v", config.DEFAULT_BRANCH),
						New("git pull"),
						New("git checkout -"),
						New("git merge %v", config.DEFAULT_BRANCH),
						New("git stash pop"),
					}
					return RunCmds(cmds)
				},
			},
		},
	},
	{
		name:        "curated",
		description: "curated list of commands",
		run: func(param []string) error {
			cmds := [][]string{
				{"sudo !!", "run the last command as sudo"},
			}
			PrintCmds(cmds)
			return nil
		},
	},
	{
		name:        "morning",
		description: "morning script",
		run: func(params []string) error {
			cmds := []Internal{
				New("fsh login"),
				New("fsh dev pull"),
			}
			return RunCmds(cmds)
		},
	},
	{
		name:        "update",
		description: "update toolbelt",
		run: func(params []string) error {
			dir := path.Join(config.REPOS_PATH, config.REPO_NAME)
			cmds := []Internal{
				NewWithDir(dir, "git pull"),
				NewWithDir(dir, "go build"),
				NewWithDir(dir, "cp %v %v", config.EXECUTABLE_NAME, config.CLI_PATH),
			}
			return RunCmds(cmds)
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
		if cmd == nil || cmd.children == nil || len(cmd.children) == 0 {
			break
		}
		curr = cmd.children
	}
	return cmd.run(input[i:])
}

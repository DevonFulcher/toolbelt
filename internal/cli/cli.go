package cli

import (
	"fmt"
	"os"
	"path"
	"strconv"
	"toolbelt/internal/config"
	"toolbelt/pkg/fs"
	"toolbelt/pkg/git"
	"toolbelt/pkg/shell"
	"toolbelt/pkg/vscode"
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
				run: func(params []string) error {
					return git.GitSave(".", params[0])
				},
			},
			{
				name:        "sync",
				description: "sync changes from main into branch",
				run: func(params []string) error {
					cmd := shell.New("git add -A")
					_, err := cmd.RunCmd()
					if err != nil {
						return err
					}
					cmd = shell.New("git diff --cached --numstat | wc -l")
					stdout, err := cmd.RunCmd()
					if err != nil {
						return err
					}
					numStashedFiles, err := strconv.Atoi(stdout)
					if err != nil {
						return err
					}
					cmds := []shell.Internal{
						shell.New("git stash"),
						shell.New("git checkout %v", config.DEFAULT_BRANCH),
						shell.New("git pull"),
						shell.New("git checkout -"),
						shell.New("git merge %v", config.DEFAULT_BRANCH),
					}
					_, err = shell.RunCmds(cmds)
					if err != nil {
						return err
					}
					if numStashedFiles > 0 {
						cmd = shell.New("git stash pop")
						_, err = cmd.RunCmd()
						if err != nil {
							return err
						}
					}
					return nil
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
		name:        "curated",
		description: "curated list of commands",
		run: func(params []string) error {
			cmds := [][]string{
				{"sudo !!", "run the last command as sudo"},
			}
			shell.PrintCmds(cmds)
			return nil
		},
	},
	{
		name:        "morning",
		description: "morning script",
		run: func(params []string) error {
			c := shell.New("aws sso login")
			_, err := c.RunCmd()
			if err != nil {
				return err
			}
			return git.PullRepos()
		},
	},
	{
		name:        "update",
		description: "update toolbelt",
		run: func(params []string) error {
			dir := path.Join(config.REPOS_PATH, config.REPO_NAME)
			cmds := []shell.Internal{
				shell.NewWithDir(dir, "git pull"),
				shell.NewWithDir(dir, "go build"),
				shell.NewWithDir(dir, "cp %v %v", config.EXECUTABLE_NAME, config.CLI_PATH),
			}
			_, err := shell.RunCmds(cmds)
			if err != nil {
				return err
			}
			return nil
		},
	},
	{
		name:        "kill",
		description: "kill a process for a given port",
		run: func(params []string) error {
			c := shell.New("kill $(lsof -t -i:%v)", params[0])
			_, err := c.RunCmd()
			if err != nil {
				return err
			}
			return nil
		},
	},
	{
		name:        "devspace",
		description: "utilities for devspace",
		children: []External{
			{
				name:        "reset",
				description: "reset devspace",
				run: func(params []string) error {
					err := os.Remove("~/.devspace")
					if err != nil {
						return err
					}
					cmds := []shell.Internal{
						shell.New("fsh dev destroy %v", config.DEVSPACE_NAMESPACE),
						shell.New("devspace use namespace %v", config.DEVSPACE_NAMESPACE),
					}
					_, err = shell.RunCmds(cmds)
					if err != nil {
						return err
					}
					return nil
				},
			},
		},
	},
	{
		name:        "dot",
		description: "utilities for dotfiles",
		children: []External{
			{
				name:        "pull",
				description: "pull in dotfile changes",
				run: func(params []string) error {
					err := git.CloneIfNotExist(config.REPOS_PATH, config.GITHUB_USERNAME, config.DOTFILES_REPO)
					if err != nil {
						return err
					}

					dotfiles := path.Join(config.REPOS_PATH, config.DOTFILES_REPO)
					c := shell.NewWithDir(dotfiles, "git pull")
					_, err = c.RunCmd()
					if err != nil {
						return err
					}

					err = fs.CopyFile(config.VSCODE_DOTFILES_SETTINGS, config.VSCODE_USER_SETTINGS)
					if err != nil {
						return err
					}

					return vscode.PullExtensions()
				},
			},
			{
				name:        "push",
				description: "push dotfile changes",
				run: func(params []string) error {
					err := git.CloneIfNotExist(config.REPOS_PATH, config.GITHUB_USERNAME, config.DOTFILES_REPO)
					if err != nil {
						return err
					}

					err = fs.CopyFile(config.VSCODE_USER_SETTINGS, config.VSCODE_DOTFILES_SETTINGS)
					if err != nil {
						return err
					}

					return git.GitSave(config.DOTFILES_PATH, "dot files push")
				},
			},
			{
				name:        "list",
				description: "list dot files",
				run: func(params []string) error {
					fmt.Printf("vscode: %v", config.VSCODE_USER_SETTINGS)
					return nil
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
	return nil, fmt.Errorf("invalid input. %v is not valid", input)
}

func printDescription(cmds []External) {
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
	if cmd.run == nil {
		printDescription(cmd.children)
		return nil
	}
	return cmd.run(input[i:])
}

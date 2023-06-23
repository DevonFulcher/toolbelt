package cmd

import (
	"fmt"
	"net/http"
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

type authedTransport struct {
	key     string
	wrapped http.RoundTripper
}

func (t *authedTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	req.Header.Set("Authorization", "bearer "+t.key)
	return t.wrapped.RoundTrip(req)
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
					return gitSave(params[0])
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
			{
				name:        "pull",
				description: "pull all repos in the repos folder",
				run: func(params []string) error {
					return pullRepos()
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
			PrintCmds(cmds)
			return nil
		},
	},
	{
		name:        "morning",
		description: "morning script",
		run: func(params []string) error {
			c := New("fsh login")
			err := c.RunCmd()
			if err != nil {
				return err
			}
			return pullRepos()
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
	{
		name:        "kill",
		description: "kill a process for a given port",
		run: func(params []string) error {
			c := New("kill $(lsof -t -i:%v)", params[0])
			return c.RunCmd()
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
					cmds := []Internal{
						New("fsh dev destroy dev-devonfulcher"),
						New("devspace use namespace dev-devonfulcher"),
					}
					return RunCmds(cmds)
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
					err := cloneIfNotExist(config.REPOS_PATH, config.GITHUB_USERNAME, config.DOTFILES_REPO)
					if err != nil {
						return err
					}

					dotfiles := path.Join(config.REPOS_PATH, config.DOTFILES_REPO)
					c := NewWithDir(dotfiles, "git pull")
					err = c.RunCmd()
					if err != nil {
						return err
					}

					return copyFile(config.VSCODE_DOTFILES_SETTINGS, config.VSCODE_USER_SETTINGS)
				},
			},
			{
				name:        "push",
				description: "push dotfile changes",
				run: func(params []string) error {
					err := cloneIfNotExist(config.REPOS_PATH, config.GITHUB_USERNAME, config.DOTFILES_REPO)
					if err != nil {
						return err
					}

					err = copyFile(config.VSCODE_USER_SETTINGS, config.VSCODE_DOTFILES_SETTINGS)
					if err != nil {
						return err
					}

					return gitSave("dot files push")
				},
			},
		},
	},
}

func gitSave(message string) error {
	cmds := []Internal{}
	path, _ := os.Getwd()
	if strings.Contains(path, config.SLG_REPO) {
		cmds = append(cmds, New("gradle ktlintFormat"))
	}
	cmds = append(cmds, []Internal{
		New("git add -A"),
		NewFromArray([]string{"git", "commit", "-m", message}),
		New("git push"),
	}...)
	return RunCmds(cmds)
}

func cloneIfNotExist(parentDirPath string, org string, repo string) error {
	repoCloneArg := fmt.Sprintf("git@github.com:%v/%v.git", org, repo)
	repoPath := path.Join(parentDirPath, repo)
	if _, err := os.Stat(repoPath); os.IsNotExist(err) {
		c := NewWithDir(parentDirPath, "git clone %v", repoCloneArg)
		err := c.RunCmd()
		if err != nil {
			return err
		}
	}
	return nil
}

func copyFile(src string, dest string) error {
	bytes, err := os.ReadFile(src)
	if err != nil {
		return err
	}
	err = os.WriteFile(dest, bytes, 0777)
	if err != nil {
		return err
	}
	return nil
}

func pullRepos() error {
	dirs, err := os.ReadDir(config.REPOS_PATH)
	if err != nil {
		return err
	}
	cmds := []Internal{}
	for _, dir := range dirs {
		cmds = append(cmds, NewWithDir(dir.Name(), "git pull"))
	}
	return RunCmdsConcurrent(cmds)
}

func findCmd(input string, cmds []External) (*External, error) {
	for _, cmd := range cmds {
		if input == cmd.name {
			return &cmd, nil
		}
	}
	return nil, fmt.Errorf("Invalid input. %v is not valid", input)
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

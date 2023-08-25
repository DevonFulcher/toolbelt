package external

import (
	"errors"
	"fmt"
	"os"
	"path"
	"strings"
	"toolbelt/config"
	"toolbelt/hooks"
	"toolbelt/internal"
)

type External struct {
	name        string
	description string
	children    []External
	run         func(params []string) error
}

type CmdRunner struct {
	hooks *hooks.Cmd
}

func NewCmdRunner(hooks *hooks.Cmd) CmdRunner {
	return CmdRunner{hooks}
}

func (r CmdRunner) getCmdTree() []External {
	return []External{
		{
			name:        "git",
			description: "git utilities",
			children: []External{
				{
					name:        "save",
					description: "save progress and push it to remote",
					run: func(params []string) error {
						return gitSave(".", params[0])
					},
				},
				{
					name:        "sync",
					description: "sync changes from main into branch",
					run: func(params []string) error {
						cmds := []internal.Cmds{
							internal.New("git add -A"),
							internal.New("git stash"),
							internal.New("git checkout %v", config.DEFAULT_BRANCH),
							internal.New("git pull"),
							internal.New("git checkout -"),
							internal.New("git merge %v", config.DEFAULT_BRANCH),
							internal.New("git stash pop"),
						}
						_, err := internal.RunCmds(cmds)
						if err != nil {
							return err
						}
						return nil
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
				internal.PrintCmds(cmds)
				return nil
			},
		},
		{
			name:        "morning",
			description: "morning script",
			run: func(params []string) error {
				r.hooks.firstMorning()
				return pullRepos()
			},
		},
		{
			name:        "update",
			description: "update toolbelt",
			run: func(params []string) error {
				dir := path.Join(config.REPOS_PATH, config.REPO_NAME)
				cmds := []internal.Cmds{
					internal.NewWithDir(dir, "git pull"),
					internal.NewWithDir(dir, "go build"),
					internal.NewWithDir(dir, "cp %v %v", config.EXECUTABLE_NAME, config.CLI_PATH),
				}
				_, err := internal.RunCmds(cmds)
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
				c := internal.New("kill $(lsof -t -i:%v)", params[0])
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
						cmds := []internal.Cmds{
							internal.New("fsh dev destroy dev-devonfulcher"),
							internal.New("devspace use namespace dev-devonfulcher"),
						}
						_, err := internal.RunCmds(cmds)
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
						err := cloneIfNotExist(config.REPOS_PATH, config.GITHUB_USERNAME, config.DOTFILES_REPO)
						if err != nil {
							return err
						}

						dotfiles := path.Join(config.REPOS_PATH, config.DOTFILES_REPO)
						c := internal.NewWithDir(dotfiles, "git pull")
						_, err = c.RunCmd()
						if err != nil {
							return err
						}

						err = copyFile(config.VSCODE_DOTFILES_SETTINGS, config.VSCODE_USER_SETTINGS)
						if err != nil {
							return err
						}

						return pullExtensions()
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

						return gitSave(config.DOTFILES_PATH, "dot files push")
					},
				},
			},
		},
	}
}

func (r *CmdRunner) Run(input []string) error {
	cmdTree := r.getCmdTree()
	if len(input) == 0 {
		printDescription(cmdTree)
		return nil
	}
	curr := cmdTree
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

func subtract[T comparable](left []T, right []T) []T {
	result := []T{}
	rightMap := make(map[T]bool)
	for _, item := range right {
		rightMap[item] = true
	}

	for _, item := range left {
		if !rightMap[item] {
			result = append(result, item)
		}
	}
	return result
}

func pullExtensions() error {
	c := internal.New("code --list-extensions")
	out, err := c.RunCmd()
	if err != nil {
		return err
	}
	prior := strings.Split(out, "\n")

	bytes, err := os.ReadFile(config.VSCODE_DOTFILES_EXTENSIONS)
	if err != nil {
		return err
	}
	remote := strings.Split(string(bytes), "\n")

	installationErrs := []string{}
	toInstall := subtract(remote, prior)
	for _, ext := range toInstall {
		c = internal.New("code --install-extension %v", ext)
		_, err = c.RunCmd()
		if err != nil {
			installationErrs = append(installationErrs, err.Error())
		}
	}

	toUninstall := subtract(prior, remote)
	for _, ext := range toUninstall {
		c = internal.New("code --uninstall-extension %v", ext)
		c.RunCmd()
		if err != nil {
			installationErrs = append(installationErrs, err.Error())
		}
	}

	return errors.New(strings.Join(installationErrs, "\n"))
}

func gitSave(dir string, message string) error {
	cmds := []internal.Cmds{}
	path, _ := os.Getwd()
	if strings.Contains(path, config.SLG_REPO) {
		cmds = append(cmds, internal.New("gradle ktlintFormat"))
	}
	cmds = append(cmds, []internal.Cmds{
		internal.NewWithDir(dir, "git add -A"),
		internal.NewFromArrayWithDir(dir, []string{"git", "commit", "-m", message}),
		internal.NewWithDir(dir, "git push"),
	}...)
	_, err := internal.RunCmds(cmds)
	if err != nil {
		return err
	}
	return nil
}

func cloneIfNotExist(parentDirPath string, org string, repo string) error {
	repoCloneArg := fmt.Sprintf("git@github.com:%v/%v.git", org, repo)
	repoPath := path.Join(parentDirPath, repo)
	if _, err := os.Stat(repoPath); os.IsNotExist(err) {
		c := internal.NewWithDir(parentDirPath, "git clone %v", repoCloneArg)
		_, err := c.RunCmd()
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
	err = os.Remove(dest)
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
	cmds := []internal.Cmds{}
	for _, dir := range dirs {
		repoPath := path.Join(config.REPOS_PATH, dir.Name())
		cmds = append(cmds, internal.NewWithDir(repoPath, "git pull"))
	}
	_, err = internal.RunCmdsConcurrent(cmds)
	if err != nil {
		return err
	}
	return nil
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

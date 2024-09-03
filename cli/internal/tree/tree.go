package tree

import (
	"toolbelt/pkg/cli"
	"toolbelt/pkg/datadog"
	"toolbelt/pkg/git"
	"toolbelt/pkg/kill"
	"toolbelt/pkg/repo"
)

var CmdTree = []cli.Command{
	{
		Name:        "git",
		Description: "git utilities",
		Children: []cli.Command{
			{
				Name:        "save",
				Description: "git add -A, git commit -m, and git push",
				Run: func(params []string) error {
					return git.Save(params)
				},
			},
		},
	},
	{
		Name:        "kill",
		Description: "kill a process for a given port",
		Run: func(params []string) error {
			return kill.Port(params)
		},
	},
	{
		Name:        "dev",
		Description: "generic development utilities",
		Children: []cli.Command{
			{
				Name:        "test",
				Description: "Run the tests",
				Run: func(params []string) error {
					return repo.Current().Test()
				},
			},
			{
				Name:        "Run",
				Description: "Run the app locally",
				Run: func(params []string) error {
					return repo.Current().Run()
				},
			},
			{
				Name:        "lint",
				Description: "Run the lint checks",
				Run: func(params []string) error {
					return repo.Current().Lint()
				},
			},
			{
				Name:        "format",
				Description: "format the repo",
				Run: func(params []string) error {
					return repo.Current().Format()
				},
			},
		},
	},
	{
		Name:        "datadog",
		Description: "tools for the observability platform DataDog",
		Run: func(params []string) error {
			return datadog.Form()
		},
	},
}

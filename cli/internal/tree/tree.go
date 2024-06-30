package tree

import (
	"toolbelt/internal/update"
	"toolbelt/pkg/cli"
	"toolbelt/pkg/datadog"
	"toolbelt/pkg/devspace"
	"toolbelt/pkg/kill"
	"toolbelt/pkg/repo"
)

var CmdTree = []cli.Command{
	{
		Name:        "update",
		Description: "update toolbelt",
		Run: func(params []string) error {
			return update.Run()
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
		Name:        "devspace",
		Description: "utilities for devspace",
		Children: []cli.Command{
			{
				Name:        "reset",
				Description: "reset devspace",
				Run: func(params []string) error {
					return devspace.Reset()
				},
			},
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

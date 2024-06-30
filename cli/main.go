package main

import (
	"fmt"
	"os"
	"toolbelt/internal/tree"
	"toolbelt/pkg/cli"
)

func main() {
	input := os.Args[1:] // ignore the "toolbelt" prefix
	err := cli.Run(input, tree.CmdTree)
	if err != nil {
		fmt.Println(err.Error())
		os.Exit(1)
	}
}

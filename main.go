package main

import (
	"fmt"
	"os"
	"toolbelt/external"
	"toolbelt/hooks"
)

func main() {
	input := os.Args[1:] // ignore the "toolbelt" prefix
	h := hooks.NewCurrent()
	cmdRunner := external.NewCmdRunner(h)
	err := cmdRunner.Run(input)
	if err != nil {
		fmt.Println(err.Error())
		os.Exit(1)
	}
	if err != nil {
		fmt.Println(err.Error())
		os.Exit(1)
	}
}

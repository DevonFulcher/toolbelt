package main

import (
	"fmt"
	"os"
	"toolbelt/cmd"
)

func main() {
	input := os.Args[1:] // ignore the "toolbelt" prefix
	err := cmd.Run(input)
	if err != nil {
		fmt.Println(err.Error())
		os.Exit(1)
	}
	if err != nil {
		fmt.Println(err.Error())
		os.Exit(1)
	}
}

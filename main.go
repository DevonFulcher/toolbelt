package main

import (
	"fmt"
	"os"
	"os/exec"
)

func Equal[T comparable](a, b []T) bool {
	if len(a) != len(b) {
		return false
	}
	for i, v := range a {
		if v != b[i] {
			return false
		}
	}
	return true
}

func main() {
	og := os.Args[1:]
	if Equal(og[:2], []string{"git", "save"}) {
		cmds := [][]string{
			{"git", "add", "-A"},
			{"git", "commit", "-m", og[2]},
			{"git", "push"},
		}
		for _, cmd := range cmds {
			fmt.Println(cmd)
			toRun := exec.Command(cmd[0], cmd[1:]...)
			toRun.Stdout = os.Stdout
			if err := toRun.Run(); err != nil {
				fmt.Println("could not run command: ", err)
			}
		}
	}
}

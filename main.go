package main

import (
	"fmt"
	"os"
	"os/exec"

	"golang.org/x/exp/constraints"
)

func Min[T constraints.Ordered](a, b T) T {
	if a < b {
		return a
	}
	return b
}

func PrefixEqual[T comparable](a, b []T) bool {
	minLength := Min(len(a), len(b))
	for i := 0; i < minLength; i++ {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func MatchCmd(og []string) error {
	if PrefixEqual(og, []string{"git", "save"}) {
		cmds := [][]string{
			{"git", "add", "-A"},
			{"git", "commit", "-m", og[2]},
			{"git", "push"},
		}
		return RunCmds(cmds)
	} else if PrefixEqual(og, []string{"curated"}) {
		cmds := [][]string{
			{"sudo !!", "run the last command as sudo"},
		}
		PrintCmds(cmds)
		return nil
	}
	return fmt.Errorf("invalid command: %v", og)
}

func RunCmds(cmds [][]string) error {
	for _, cmd := range cmds {
		fmt.Println(cmd)
		toRun := exec.Command(cmd[0], cmd[1:]...)
		toRun.Stdout = os.Stdout
		if err := toRun.Run(); err != nil {
			return fmt.Errorf("could not run command: %v with error: %v", cmd, err)
		}
	}
	return nil
}

func PrintCmds(cmds [][]string) {
	for _, cmd := range cmds {
		fmt.Println()
		fmt.Println(cmd[0])
		fmt.Printf("- %v", cmd[1])
		fmt.Println()
	}
}

func main() {
	og := os.Args[1:]
	err := MatchCmd(og)
	if err != nil {
		fmt.Println(err.Error())
		os.Exit(1)
	}
}

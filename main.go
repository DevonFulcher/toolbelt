package main

import (
	"fmt"
	"os"
	"os/exec"

	"golang.org/x/exp/constraints"
)

const DEFAULT_BRANCH = "main"

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
	} else if PrefixEqual(og, []string{"git", "log"}) {
		return RunCmd([]string{"git", "log", "--graph", "--all", "--pretty='format:%C(auto)%h %C(cyan)%ar %C(auto)%d %C(magenta)%an %C(auto)%s'"})
	} else if PrefixEqual(og, []string{"kill", "process", "on", "port"}) {
		// TODO: test this
		return RunCmd([]string{"kill", fmt.Sprintf("$(lsof -ti tcp:%v)", og[4])})
	} else if PrefixEqual(og, []string{"good", "morning"}) {
		return RunCmd([]string{"git", "standup", "-w", "MON-FRI"})
	} else if PrefixEqual(og, []string{"git", "sync"}) {
		cmds := [][]string{
			{"git", "add", "-A"},
			{"git", "stash"},
			{"git", "checkout", DEFAULT_BRANCH},
			{"git", "pull"},
			{"git", "checkout", "-"},
			{"git", "merge", DEFAULT_BRANCH},
		}
		return RunCmds(cmds)
	}
	return fmt.Errorf("invalid command: %v", og)
}

func RunCmds(cmds [][]string) error {
	for _, cmd := range cmds {
		err := RunCmd(cmd)
		if err != nil {
			return err
		}
	}
	return nil
}

func RunCmd(cmd []string) error {
	fmt.Println(cmd)
	toRun := exec.Command(cmd[0], cmd[1:]...)
	toRun.Stdout = os.Stdout
	toRun.Stdin = os.Stdin
	if err := toRun.Run(); err != nil {
		return fmt.Errorf("could not run command: %v with error: %v", cmd, err)
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

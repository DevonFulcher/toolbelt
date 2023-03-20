package main

import (
	"fmt"
	"os"
	"path"
	"toolbelt/cmd"

	"golang.org/x/exp/constraints"
)

const DEFAULT_BRANCH = "main"
const REPOS_PATH = "~/git"
const REPO_NAME = "toolbelt"
const EXECUTABLE_NAME = "toolbelt"
const CLI_PATH = "~/cli"

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
		cmds := []cmd.Cmd{
			cmd.New("git add -A"),
			cmd.New("git commit -m %v", og[2]),
			cmd.New("git push"),
		}
		return RunCmds(cmds)
	} else if PrefixEqual(og, []string{"curated"}) {
		cmds := [][]string{
			{"sudo !!", "run the last command as sudo"},
		}
		PrintCmds(cmds)
		return nil
	} else if PrefixEqual(og, []string{"git", "log"}) {
		arr := []string{"git", "log", "--graph", "--all", "--pretty='format:%C(auto)%h %C(cyan)%ar %C(auto)%d %C(magenta)%an %C(auto)%s'"}
		c := cmd.NewFromArray(arr)
		return c.RunCmd()
	} else if PrefixEqual(og, []string{"kill", "process", "on", "port"}) {
		// TODO: test this
		c := cmd.New("kill %v %v", fmt.Sprintf("$(lsof -ti tcp:%v)", og[4]))
		return c.RunCmd()
	} else if PrefixEqual(og, []string{"good", "morning"}) {
		c := cmd.New("git standup -w MON-FRI")
		return c.RunCmd()
	} else if PrefixEqual(og, []string{"git", "sync"}) {
		cmds := []cmd.Cmd{
			cmd.New("git add -A"),
			cmd.New("git stash"),
			cmd.New("git checkout %v", DEFAULT_BRANCH),
			cmd.New("git pull"),
			cmd.New("git checkout -"),
			cmd.New("git merge %v", DEFAULT_BRANCH),
		}
		return RunCmds(cmds)
	} else if PrefixEqual(og, []string{"update"}) {
		dir := path.Join(REPOS_PATH, REPO_NAME)
		cmds := []cmd.Cmd{
			cmd.NewWithDir(dir, "git pull"),
			cmd.NewWithDir(dir, "go build"),
			cmd.NewWithDir(dir, "cp %v %v", EXECUTABLE_NAME, CLI_PATH),
		}
		return RunCmds(cmds)
	}
	return fmt.Errorf("invalid command: %v", og)
}

func RunCmds(cmds []cmd.Cmd) error {
	for _, cmd := range cmds {
		err := cmd.RunCmd()
		if err != nil {
			return err
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

package main

import (
	"fmt"
	"os"
	"path"
	"toolbelt/cmd"

	"golang.org/x/exp/constraints"
)

const HOME = "/Users/devonfulcher"
const DEFAULT_BRANCH = "main"
const REPO_NAME = "toolbelt"
const EXECUTABLE_NAME = "toolbelt"
const SLG_REPO = "semantic-layer-gateway"
const RUNTIME_GATEWAY_REPO = "runtime-gateway"

var REPOS_PATH = path.Join(HOME, "git")
var CLI_PATH = path.Join(HOME, "cli")
var SLG_PATH = path.Join(REPOS_PATH, SLG_REPO)
var RUNTIME_GATEWAY_PATH = path.Join(REPOS_PATH, RUNTIME_GATEWAY_REPO)

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
		fmt.Println("asdf")
		return nil
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
		// TODO: dbt specific code
		cmds := []cmd.Internal{
			cmd.New("fsh login"),
			cmd.New("fsh dev pull"),
			cmd.NewWithDir(RUNTIME_GATEWAY_PATH, "devspace dev"),
		}
		return RunCmds(cmds)
	} else if PrefixEqual(og, []string{"git", "sync"}) {
		cmds := []cmd.Internal{
			cmd.New("git add -A"),
			cmd.New("git stash"),
			cmd.New("git checkout %v", DEFAULT_BRANCH),
			cmd.New("git pull"),
			cmd.New("git checkout -"),
			cmd.New("git merge %v", DEFAULT_BRANCH),
			cmd.New("git stash pop"),
		}
		return RunCmds(cmds)
	} else if PrefixEqual(og, []string{"update"}) {
		dir := path.Join(REPOS_PATH, REPO_NAME)
		cmds := []cmd.Internal{
			cmd.NewWithDir(dir, "git pull"),
			cmd.NewWithDir(dir, "go build"),
			cmd.NewWithDir(dir, "cp %v %v", EXECUTABLE_NAME, CLI_PATH),
		}
		return RunCmds(cmds)
	}
	return fmt.Errorf("invalid command: %v", og)
}

// TODO: remove
func RunCmds(cmds []cmd.Internal) error {
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
	input := os.Args[1:] // ignore the "toolbelt" prefix
	err := cmd.Run(input)
	if err != nil {
		fmt.Println(err.Error())
		os.Exit(1)
	}
	err = MatchCmd(input)
	if err != nil {
		fmt.Println(err.Error())
		os.Exit(1)
	}
}

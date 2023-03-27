package main

import (
	"fmt"
	"os"
	"path"
	"strings"
	"toolbelt/cmd"

	"golang.org/x/exp/constraints"
)

const HOME = "/Users/devonfulcher"
const DEFAULT_BRANCH = "main"
const REPO_NAME = "toolbelt"
const EXECUTABLE_NAME = "toolbelt"
const SLG_REPO = "semantic-layer-gateway"

var REPOS_PATH = path.Join(HOME, "git")
var CLI_PATH = path.Join(HOME, "cli")
var SLG_PATH = path.Join(REPOS_PATH, SLG_REPO)

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
		cmds := []cmd.Cmd{}
		path, _ := os.Getwd()
		if strings.Contains(path, SLG_REPO) {
			cmds = append(cmds, cmd.New("gradle ktlintFormat"))
		}
		cmds = append(cmds, []cmd.Cmd{
			cmd.New("git add -A"),
			cmd.NewFromArray([]string{"git", "commit", "-m", og[2]}),
			cmd.New("git push"),
		}...)
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
		// TODO: dbt specific code
		cmds := []cmd.Cmd{
			cmd.New("fsh login"),
			cmd.New("fsh dev pull"),
			cmd.NewWithDir(SLG_PATH, "git standup -w MON-FRI"),
		}
		return RunCmds(cmds)
	} else if PrefixEqual(og, []string{"git", "sync"}) {
		// TODO: dbt specific code
		cmds := []cmd.Cmd{
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

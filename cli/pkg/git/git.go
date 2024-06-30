package git

import (
	"fmt"
	"os"
	"path"
	"toolbelt/pkg/shell"
)

func GitSave(dir string, message string) error {
	cmds := []shell.Cmd{}
	cmds = append(cmds, []shell.Cmd{
		shell.NewWithDir(dir, "git add -A"),
		shell.NewFromArrayWithDir(dir, []string{"git", "commit", "-m", message}),
		shell.NewWithDir(dir, "git push"),
	}...)
	_, err := shell.RunCmds(cmds)
	if err != nil {
		return err
	}
	return nil
}

func CloneIfNotExist(parentDirPath string, org string, repo string) error {
	repoCloneArg := fmt.Sprintf("git@github.com:%v/%v.git", org, repo)
	repoPath := path.Join(parentDirPath, repo)
	if _, err := os.Stat(repoPath); os.IsNotExist(err) {
		c := shell.NewWithDir(parentDirPath, "git clone %v", repoCloneArg)
		_, err := c.RunCmd()
		if err != nil {
			return err
		}
	}
	return nil
}

func Save(params []string) error {
	//commitCmd := fmt.Sprintf("git commit -m \"%s\"", "testing again again")
	commitCmd := "git commit -m \"test\""
	fmt.Println(commitCmd)
	_, err := shell.RunCmdsFromStr(
		//"git add -A", fmt.Sprintf("git commit -m \"%s\"", "testing again again"), "git push",
		"git add -A", commitCmd, "git push",
	)
	return err
}

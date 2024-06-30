package git

import (
	"fmt"
	"os"
	"toolbelt/pkg/shell"
)

func Save(params []string) error {
	dir, _ := os.Getwd()
	_, err := shell.RunCmdsFromStr(
		dir,
		"git add -A",
		fmt.Sprintf("git commit -m %q", params[0]),
		"git push",
	)
	return err
}

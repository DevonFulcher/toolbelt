package git

import (
	"fmt"
	"toolbelt/pkg/shell"
)

func Save(params []string) error {
	_, err := shell.RunCmdsFromStr(
		"git add -A",
		fmt.Sprintf("git commit -m %q", params[0]),
		"git push",
	)
	return err
}

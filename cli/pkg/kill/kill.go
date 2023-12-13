package kill

import (
	"fmt"
	"toolbelt/pkg/shell"
)

func Port(params []string) error {
	c := shell.New("lsof -t -i:%v", params[0])
	_, err := c.RunCmd()
	if err != nil {
		return fmt.Errorf("couldn't run run `lsof -t -i:%v`. port is likely not in use", params[0])
	}
	c = shell.New("kill $(lsof -t -i:%v)", params[0])
	_, err = c.RunCmd()
	if err != nil {
		return err
	}
	return nil
}

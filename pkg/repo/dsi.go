package repo

import "toolbelt/pkg/shell"

type DbtSemanticInterfaces struct{}

func (r DbtSemanticInterfaces) Reviewers() []string {
	return []string{
		"plypaul",
		"tlento",
		"QMalcolm",
	}
}

func (r DbtSemanticInterfaces) Test() error {
	c := shell.New("make test")
	_, err := c.RunCmd()
	return err
}

func (r DbtSemanticInterfaces) Run() error {
	c := shell.New("test")
	_, err := c.RunCmd()
	return err
}

func (r DbtSemanticInterfaces) Lint() error {
	c := shell.New("test")
	_, err := c.RunCmd()
	return err
}

func (r DbtSemanticInterfaces) Format() error {
	c := shell.New("test")
	_, err := c.RunCmd()
	return err
}

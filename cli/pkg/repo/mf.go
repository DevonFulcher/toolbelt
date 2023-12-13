package repo

import "toolbelt/pkg/shell"

type Metricflow struct{}

func (r Metricflow) Reviewers() []string {
	return []string{
		"courtneyholcomb",
		"plypaul",
		"tlento",
	}
}

func (r Metricflow) Test() error {
	c := shell.New("make test")
	_, err := c.RunCmd()
	return err
}

func (r Metricflow) Run() error {
	c := shell.New("test")
	_, err := c.RunCmd()
	return err
}

func (r Metricflow) Lint() error {
	c := shell.New("test")
	_, err := c.RunCmd()
	return err
}

func (r Metricflow) Format() error {
	c := shell.New("test")
	_, err := c.RunCmd()
	return err
}

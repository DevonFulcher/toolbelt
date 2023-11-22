package repo

import "toolbelt/pkg/shell"

type MetricflowServer struct{}

func (r MetricflowServer) Reviewers() []string {
	return []string{
		"courtneyholcomb",
		"WilliamDee",
	}
}

func (r MetricflowServer) Test() error {
	c := shell.New("make test")
	_, err := c.RunCmd()
	return err
}

func (r MetricflowServer) Run() error {
	c := shell.New("test")
	_, err := c.RunCmd()
	return err
}

func (r MetricflowServer) Lint() error {
	c := shell.New("test")
	_, err := c.RunCmd()
	return err
}

func (r MetricflowServer) Format() error {
	c := shell.New("test")
	_, err := c.RunCmd()
	return err
}

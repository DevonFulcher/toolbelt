package repo

import "toolbelt/pkg/shell"

type SemanticLayerGateway struct{}

func (r SemanticLayerGateway) Reviewers() []string {
	return []string{
		"emmack",
		"aiguofer",
	}
}

func (r SemanticLayerGateway) Test() error {
	c := shell.New("test")
	_, err := c.RunCmd()
	return err
}

func (r SemanticLayerGateway) Run() error {
	c := shell.New("test")
	_, err := c.RunCmd()
	return err
}

func (r SemanticLayerGateway) Lint() error {
	c := shell.New("test")
	_, err := c.RunCmd()
	return err
}

func (r SemanticLayerGateway) Format() error {
	c := shell.New("test")
	_, err := c.RunCmd()
	return err
}
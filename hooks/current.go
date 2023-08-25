package hooks

import "toolbelt/internal"

type Current struct{}

func NewCurrent() Current {
	return Current{}
}

func (h *Current) FirstMorning() error {
	c := internal.New("fsh login")
	_, err := c.RunCmd()
	return err
}

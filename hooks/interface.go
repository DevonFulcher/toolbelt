package hooks

type Cmd interface {
	FirstMorning() error
}

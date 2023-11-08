package repo

type DbtSemanticInterfaces struct{}

func (r DbtSemanticInterfaces) Reviewers() []string {
	return []string{
		"plypaul",
		"tlento",
		"QMalcolm",
	}
}

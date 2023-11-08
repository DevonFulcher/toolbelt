package repo

type Metricflow struct{}

func (r Metricflow) Reviewers() []string {
	return []string{
		"courtneyholcomb",
		"plypaul",
		"tlento",
	}
}

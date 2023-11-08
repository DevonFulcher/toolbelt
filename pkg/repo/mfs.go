package repo

type MetricflowServer struct{}

func (r MetricflowServer) Reviewers() []string {
	return []string{
		"courtneyholcomb",
		"WilliamDee",
	}
}

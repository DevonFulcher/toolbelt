package repo

type SemanticLayerGateway struct{}

func (r SemanticLayerGateway) Reviewers() []string {
	return []string{
		"emmack",
		"aiguofer",
	}
}

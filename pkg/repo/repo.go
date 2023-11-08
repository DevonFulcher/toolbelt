package repo

import (
	"fmt"
	"os"
	"strings"
)

type Repo interface {
	Reviewers() []string
}

func Current() Repo {
	directory, err := os.Getwd()
	if err != nil {
		fmt.Println(err)
	}
	if strings.Contains(directory, "metricflow-server") {
		return MetricflowServer{}
	}
	if strings.Contains(directory, "metricflow") {
		return Metricflow{}
	}
	if strings.Contains(directory, "dbt-semantic-interfaces") {
		return DbtSemanticInterfaces{}
	}
	if strings.Contains(directory, "semantic-layer-gateway") {
		return SemanticLayerGateway{}
	}
	return nil
}

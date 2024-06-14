package datadog

import (
	"fmt"
	"net/url"
	"strconv"
	"strings"
	"time"
	"toolbelt/pkg/browser"
	"toolbelt/pkg/comparable"

	"github.com/charmbracelet/huh"
)

func getStructuredLogQuery(service string, key string, value string) string {
	if service == "semantic-layer-gateway" {
		return fmt.Sprintf("@%v:%v", key, value)
	} else if service == "metricflow-server" || service == "semantic-layer-gsheets" {
		return fmt.Sprintf("@extra.%v:%v", key, value)
	}
	return ""
}

func getTimeRangeUnixTimestamps(timeRange string) (int64, int64) {
	granularity := strings.Split(timeRange, "-")
	intValue, _ := strconv.Atoi(granularity[0])
	var timeGrain time.Duration
	if granularity[1] == "minute" {
		timeGrain = time.Minute
	} else if granularity[1] == "hour" {
		timeGrain = time.Hour
	} else if granularity[1] == "day" {
		timeGrain = time.Hour * 24
	}
	now := time.Now()
	start := now.Add(-(timeGrain * time.Duration(intValue))).UnixMilli()
	return start, now.UnixMilli()
}

func getQueryUrlParam(query []string) string {
	encodedQuery := url.QueryEscape(strings.TrimRight(strings.Join(query, " "), " "))
	var queryUrlParam = ""
	if encodedQuery != "" {
		queryUrlParam = fmt.Sprintf("query=%v&", encodedQuery)
	}
	return queryUrlParam
}

func getStatuses(pages []string) ([]string, []string, error) {
	var (
		logStatus   []string
		traceStatus []string
	)
	fields := []huh.Field{}
	if comparable.Includes(pages, "logs") {
		field := huh.NewMultiSelect[string]().
			Title("Log Status").
			Options(
				huh.NewOption("Info", "info"),
				huh.NewOption("Warn", "warn"),
				huh.NewOption("Error", "error"),
			).
			Value(&logStatus)
		fields = append(fields, field)
	}
	if comparable.Includes(pages, "traces") {
		field := huh.NewMultiSelect[string]().
			Title("Trace Status").
			Options(
				huh.NewOption("Ok", "ok"),
				huh.NewOption("Error", "error"),
			).
			Value(&traceStatus)
		fields = append(fields, field)
	}
	form := huh.NewForm(
		huh.NewGroup(fields...),
	)
	err := form.Run()
	if err != nil {
		return nil, nil, err
	}
	return logStatus, traceStatus, nil
}

func Form() error {
	var (
		envId           string
		accountId       string
		services        []string
		datadogInstance string
		errorMessage    string
		timeRange       string
		pages           []string
	)

	form := huh.NewForm(
		huh.NewGroup(
			huh.NewInput().Title("Environment Id").Value(&envId),
			huh.NewInput().Title("Account Id").Value(&accountId),
			huh.NewMultiSelect[string]().
				Title("Service").
				Options(
					huh.NewOption("Metricflow Server", "metricflow-server"),
					huh.NewOption("Semantic Layer Gateway", "semantic-layer-gateway"),
					huh.NewOption("Elastic Load Balancer", "elb"),
					huh.NewOption("Google Sheets", "semantic-layer-gsheets"),
				).Value(&services),
			huh.NewSelect[string]().
				Title("DataDog Instance").
				Options(
					huh.NewOption("Multi-Tenant", "dbtlabsmt").Selected(true),
					huh.NewOption("AWS Single-Tenant", "dbtlabsstaws"),
					huh.NewOption("Azure Single-Tenant", "dbtlabsstazure"),
				).
				Validate(func(value string) error {
					if value == "" {
						return fmt.Errorf("must set DataDog instance")
					}
					return nil
				}).
				Value(&datadogInstance),
			huh.NewSelect[string]().
				Title("Time Range").
				Options(
					huh.NewOption("Live", "live"),
					huh.NewOption("Past 15 minutes", "15-minute"),
					huh.NewOption("Past 1 hour", "1-hour"),
					huh.NewOption("Past 4 hours", "4-hour"),
					huh.NewOption("Past 1 day", "1-day"),
					huh.NewOption("Past 2 days", "2-day"),
					huh.NewOption("Past 3 days", "3-day"),
					huh.NewOption("Past 7 days", "7-day"),
					huh.NewOption("Past 15 days", "15-day"),
				).
				Value(&timeRange),
			huh.NewMultiSelect[string]().
				Title("Page").
				Options(
					huh.NewOption("Logs", "logs"),
					huh.NewOption("Traces", "traces"),
				).Value(&pages),
			huh.NewText().
				Title("Error Message").
				Value(&errorMessage),
		),
	)
	err := form.Run()
	if err != nil {
		return err
	}

	logStatus, traceStatus, err := getStatuses(pages)
	if err != nil {
		return err
	}

	query := []string{}
	if len(services) > 0 {
		expression := strings.Join(services, " OR ")
		query = append(query, fmt.Sprintf("service:(%v)", expression))
	}
	structuredLogQueries := []string{}
	for _, service := range services {
		if envId != "" {
			structuredLogQueries = append(
				structuredLogQueries, getStructuredLogQuery(service, "environment_id", envId),
			)
		}
		if accountId != "" {
			structuredLogQueries = append(
				structuredLogQueries, getStructuredLogQuery(service, "account_id", accountId),
			)
		}
	}
	if len(structuredLogQueries) > 0 {
		query = append(query, "("+strings.Join(structuredLogQueries, " OR ")+")")
	}
	if errorMessage != "" {
		query = append(query, errorMessage+" ")
	}
	if comparable.Includes(pages, "logs") {
		logsQuery := make([]string, len(query))
		copy(logsQuery, query)
		if len(logStatus) > 0 {
			expression := strings.Join(logStatus, " OR ")
			logsQuery = append(logsQuery, fmt.Sprintf("status:(%v)", expression))
		}
		queryUrlParam := getQueryUrlParam(logsQuery)
		timeRangeUrlParam := ""
		liveTail := ""
		if timeRange == "live" {
			liveTail = "/livetail"
		} else {
			start, end := getTimeRangeUnixTimestamps(timeRange)
			timeRangeUrlParam = fmt.Sprintf("from_ts=%v&to_ts=%v&", start, end)
		}
		logsUrl := fmt.Sprintf("https://%v.datadoghq.com/logs%v?%v%v", datadogInstance, liveTail, timeRangeUrlParam, queryUrlParam)
		browser.Open(logsUrl)
	}
	if comparable.Includes(pages, "traces") {
		if len(traceStatus) > 0 {
			expression := strings.Join(traceStatus, " OR ")
			query = append(query, fmt.Sprintf("status:(%v)", expression))
		}
		queryUrlParam := getQueryUrlParam(query)
		timeRangeUrlParam := ""
		historicalData := true
		if timeRange == "live" {
			historicalData = false
		} else {
			start, end := getTimeRangeUnixTimestamps(timeRange)
			timeRangeUrlParam = fmt.Sprintf("start=%v&end=%v&", start, end)
		}
		tracesUrl := fmt.Sprintf("https://%v.datadoghq.com/apm/traces?%v%vhistoricalData=%v", datadogInstance, timeRangeUrlParam, queryUrlParam, historicalData)
		browser.Open(tracesUrl)
	}
	return nil
}

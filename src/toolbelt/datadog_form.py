import time
import webbrowser
from typing import List, Tuple
from urllib.parse import quote

from textual.app import App, ComposeResult
from textual.containers import Container
from textual.screen import Screen
from textual.widgets import Button, Input, Label, Select, SelectionList


def get_structured_log_query(service: str, key: str, value: str) -> str:
    if service == "semantic-layer-gateway":
        return f"@{key}:{value}"
    elif service in ["metricflow-server", "semantic-layer-gsheets"]:
        return f"@extra.{key}:{value}"
    return ""


def get_time_range_unix_timestamps(time_range: str) -> Tuple[int, int]:
    amount, unit = time_range.split("-")
    amount_int = int(amount)

    multiplier = {"minute": 60, "hour": 3600, "day": 86400}[unit]

    now_ms = int(time.time() * 1000)
    start_ms = now_ms - (amount_int * multiplier * 1000)
    return start_ms, now_ms


def get_query_url_param(query: List[str]) -> str:
    encoded_query = quote(" ".join(query).rstrip())
    return f"query={encoded_query}&" if encoded_query else ""


class DatadogForm(Screen):
    CSS = """
    Container {
        layout: vertical;
        padding: 1;
        height: auto;
        width: 100%;
    }

    Label {
        margin: 1 0;
    }
    """

    def compose(self) -> ComposeResult:
        yield Container(
            Label("DataDog Form"),
            Input(placeholder="Environment Id", id="env_id"),
            Input(placeholder="Account Id", id="account_id"),
            SelectionList(
                ("Metricflow Server", "metricflow-server"),
                ("Semantic Layer Gateway", "semantic-layer-gateway"),
                ("Elastic Load Balancer", "elb"),
                ("Google Sheets", "semantic-layer-gsheets"),
                id="services",
                name="Select Services",
            ),
            Select(
                [
                    (name, value)
                    for name, value in [
                        ("Multi-Tenant", "dbtlabsmt"),
                        ("AWS Single-Tenant", "dbtlabsstaws"),
                        ("Azure Single-Tenant", "dbtlabsstazure"),
                    ]
                ],
                id="datadog_instance",
                prompt="Select DataDog Instance",
                value="dbtlabsmt",
            ),
            Select(
                [
                    (name, value)
                    for name, value in [
                        ("Live", "live"),
                        ("Past 15 minutes", "15-minute"),
                        ("Past 1 hour", "1-hour"),
                        ("Past 4 hours", "4-hour"),
                        ("Past 1 day", "1-day"),
                        ("Past 2 days", "2-day"),
                        ("Past 3 days", "3-day"),
                        ("Past 7 days", "7-day"),
                        ("Past 15 days", "15-day"),
                    ]
                ],
                id="time_range",
                prompt="Select Time Range",
                value="15-minute",
            ),
            SelectionList(
                ("Logs", "logs"),
                ("Traces", "traces"),
                id="pages",
                name="Select Pages",
            ),
            Input(placeholder="Error Message", id="error_message"),
            SelectionList(
                ("Info", "info"),
                ("Warn", "warn"),
                ("Error", "error"),
                id="log_status",
                name="Select Log Status",
            ),
            SelectionList(
                ("Ok", "ok"),
                ("Error", "error"),
                id="trace_status",
                name="Select Trace Status",
            ),
            Button("Submit", id="submit"),
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "submit":
            self.handle_submit()

    def handle_submit(self) -> None:
        env_id = self.query_one("#env_id", Input).value
        account_id = self.query_one("#account_id", Input).value
        services = self.query_one("#services", SelectionList).selected
        datadog_instance = self.query_one("#datadog_instance", Select).value
        time_range = self.query_one("#time_range", Select).value
        assert isinstance(time_range, str)
        pages = self.query_one("#pages", SelectionList).selected
        error_message = self.query_one("#error_message", Input).value
        log_status = self.query_one("#log_status", SelectionList).selected
        trace_status = self.query_one("#trace_status", SelectionList).selected

        query = []
        if services:
            expression = " OR ".join(services)
            query.append(f"service:({expression})")

        structured_log_queries = []
        for service in services:
            if env_id:
                structured_log_queries.append(
                    get_structured_log_query(service, "environment_id", env_id)
                )
            if account_id:
                structured_log_queries.append(
                    get_structured_log_query(service, "account_id", account_id)
                )

        if structured_log_queries:
            query.append("(" + " OR ".join(structured_log_queries) + ")")

        if error_message:
            query.append(f"{error_message} ")

        if "logs" in pages:
            logs_query = query.copy()
            if log_status:
                expression = " OR ".join(log_status)
                logs_query.append(f"status:({expression})")

            query_url_param = get_query_url_param(logs_query)
            time_range_url_param = ""
            live_tail = ""

            if time_range == "live":
                live_tail = "/livetail"
            else:
                start, end = get_time_range_unix_timestamps(time_range)
                time_range_url_param = f"from_ts={start}&to_ts={end}&"

            logs_url = f"https://{datadog_instance}.datadoghq.com/logs{live_tail}?{time_range_url_param}{query_url_param}"
            webbrowser.open(logs_url)

        if "traces" in pages:
            if trace_status:
                expression = " OR ".join(trace_status)
                query.append(f"status:({expression})")

            query_url_param = get_query_url_param(query)
            time_range_url_param = ""
            historical_data = True

            if time_range == "live":
                historical_data = False
            else:
                start, end = get_time_range_unix_timestamps(time_range)
                time_range_url_param = f"start={start}&end={end}&"

            traces_url = f"https://{datadog_instance}.datadoghq.com/apm/traces?{time_range_url_param}{query_url_param}historicalData={str(historical_data).lower()}"
            webbrowser.open(traces_url)
        self.app.exit()


class DatadogApp(App):
    def on_mount(self) -> None:
        self.push_screen(DatadogForm())


def form() -> None:
    app = DatadogApp()
    app.run()


if __name__ == "__main__":
    form()

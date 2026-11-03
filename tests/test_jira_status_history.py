from datetime import UTC, datetime, timedelta

import httpx

from toolbelt.tasks.jira import JiraTracker


def test_status_history_reads_the_latest_bounded_changelog_pages():
    created = datetime(2024, 1, 1, tzinfo=UTC)
    starts: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/changelog"):
            start_at = int(request.url.params["startAt"])
            starts.append(start_at)
            if request.url.params["maxResults"] == "1":
                return httpx.Response(200, json={"total": 502, "values": []})
            end_at = min(start_at + int(request.url.params["maxResults"]), 502)
            return httpx.Response(
                200,
                json={
                    "total": 502,
                    "values": [
                        {
                            "id": str(index),
                            "created": (created + timedelta(days=index)).isoformat(),
                            "items": [
                                {
                                    "field": "status",
                                    "fromString": f"Status {index - 1}",
                                    "toString": f"Status {index}",
                                }
                            ],
                        }
                        for index in range(start_at, end_at)
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "fields": {
                    "created": created.isoformat(),
                    "status": {"name": "Current"},
                }
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handle))
    tracker = JiraTracker(
        base_url="https://x.atlassian.net",
        email="me@example.com",
        env={"JIRA_API_TOKEN": "token"},
        client=client,
    )

    history = tracker.status_history("RD-100")

    assert history is not None
    assert history.current_status == "Current"
    assert history.truncated_before
    assert len(history.changes) == 500
    assert history.changes[0].history_id == "2"
    assert history.changes[-1].history_id == "501"
    assert starts == [0, 2, 102, 202, 302, 402]


def test_status_history_returns_none_for_an_invalid_reference_without_http():
    requested: list[httpx.Request] = []
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: requested.append(request) or httpx.Response(500)
        )
    )
    tracker = JiraTracker(
        base_url="https://x.atlassian.net",
        env={"JIRA_API_TOKEN": "token"},
        client=client,
    )

    assert tracker.status_history("not-a-ticket") is None
    assert requested == []

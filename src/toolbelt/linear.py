from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import httpx


class LinearGraphQLError(RuntimeError):
    def __init__(self, message: str, *, errors: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.errors = errors or []


def _parse_linear_datetime(raw: str) -> datetime:
    # Linear returns ISO strings like "2025-01-01T12:34:56.789Z"
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _post_graphql(
    client: httpx.Client,
    *,
    query: str,
    variables: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = client.post(
        "https://api.linear.app/graphql",
        json={"query": query, "variables": variables or {}},
        timeout=20.0,
    )
    response.raise_for_status()
    payload = response.json()
    if "errors" in payload and payload["errors"]:
        messages = ", ".join(
            [
                str(e.get("message", "Unknown Linear GraphQL error"))
                for e in payload["errors"]
            ]
        )
        raise LinearGraphQLError(messages, errors=payload["errors"])
    data = payload.get("data")
    if not isinstance(data, dict):
        raise LinearGraphQLError("Linear GraphQL response missing data")
    return data


@dataclass(frozen=True)
class LinearIssueChange:
    created_at: datetime
    type: str
    actor: Optional[str]
    from_value: Optional[str]
    to_value: Optional[str]
    data: Optional[str]


@dataclass(frozen=True)
class LinearIssueWithChanges:
    identifier: str
    title: str
    url: str
    updated_at: datetime
    changes: list[LinearIssueChange]


def _get_viewer_id(client: httpx.Client) -> str:
    data = _post_graphql(client, query="query { viewer { id } }")
    viewer = data.get("viewer")
    if not isinstance(viewer, dict) or not viewer.get("id"):
        raise LinearGraphQLError("Failed to resolve Linear viewer id")
    return str(viewer["id"])


def get_in_progress_issues_with_changes_since(
    *,
    api_key: str,
    since: datetime,
    max_issues: int = 50,
    history_first: int = 25,
) -> list[LinearIssueWithChanges]:
    """
    Fetch issues assigned to the current Linear user that are in-progress ("started"),
    and include their change history entries since `since`.

    Env/creds are intentionally not read here—callers decide how to provide the API key.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    with httpx.Client(headers=headers) as client:
        viewer_id = _get_viewer_id(client)

        # NOTE: Linear's filter schema can vary across API versions/workspaces.
        # We try a history-enabled query first (to list concrete changes), and
        # gracefully fall back to a history-less query (tickets only) if needed.
        query_with_history = """
        query Issues($assigneeId: ID!, $first: Int!, $after: String, $historyFirst: Int!) {
          issues(
            first: $first
            after: $after
            filter: {
              assignee: { id: { eq: $assigneeId } }
              state: { type: { eq: "started" } }
            }
          ) {
            nodes {
              identifier
              title
              url
              updatedAt
              history(first: $historyFirst) {
                nodes {
                  createdAt
                  type
                  from
                  to
                  data
                  actor { name }
                }
              }
            }
            pageInfo {
              hasNextPage
              endCursor
            }
          }
        }
        """

        query_without_history = """
        query Issues($assigneeId: ID!, $first: Int!, $after: String) {
          issues(
            first: $first
            after: $after
            filter: {
              assignee: { id: { eq: $assigneeId } }
              state: { type: { eq: "started" } }
            }
          ) {
            nodes {
              identifier
              title
              url
              updatedAt
            }
            pageInfo {
              hasNextPage
              endCursor
            }
          }
        }
        """

        issues: list[LinearIssueWithChanges] = []
        after: str | None = None
        query = query_with_history
        history_enabled = True

        while len(issues) < max_issues:
            first = min(50, max_issues - len(issues))
            variables: dict[str, Any] = {
                "assigneeId": viewer_id,
                "first": first,
                "after": after,
            }
            if history_enabled:
                variables["historyFirst"] = history_first

            try:
                data = _post_graphql(client, query=query, variables=variables)
            except LinearGraphQLError:
                if history_enabled:
                    history_enabled = False
                    query = query_without_history
                    continue
                raise

            issues_payload = data.get("issues")
            if not isinstance(issues_payload, dict):
                break

            nodes = issues_payload.get("nodes", [])
            if not isinstance(nodes, list) or not nodes:
                break

            for node in nodes:
                if not isinstance(node, dict):
                    continue
                identifier = str(node.get("identifier") or "").strip()
                title = str(node.get("title") or "").strip()
                url = str(node.get("url") or "").strip()
                updated_at_raw = node.get("updatedAt")
                if (
                    not identifier
                    or not title
                    or not url
                    or not isinstance(updated_at_raw, str)
                ):
                    continue
                updated_at = _parse_linear_datetime(updated_at_raw)

                history_nodes: list[Any] = []
                if history_enabled:
                    history = node.get("history")
                    if isinstance(history, dict) and isinstance(
                        history.get("nodes"), list
                    ):
                        history_nodes = history["nodes"]

                changes: list[LinearIssueChange] = []
                for h in history_nodes:
                    if not isinstance(h, dict):
                        continue
                    created_at_raw = h.get("createdAt")
                    if not isinstance(created_at_raw, str):
                        continue
                    created_at = _parse_linear_datetime(created_at_raw)
                    if created_at < since:
                        continue

                    actor_name: Optional[str] = None
                    actor = h.get("actor")
                    if isinstance(actor, dict):
                        name = actor.get("name")
                        if isinstance(name, str) and name.strip():
                            actor_name = name.strip()

                    def _maybe_str(v: Any) -> Optional[str]:
                        if v is None:
                            return None
                        if isinstance(v, str):
                            s = v.strip()
                            return s or None
                        return str(v)

                    changes.append(
                        LinearIssueChange(
                            created_at=created_at,
                            type=str(h.get("type") or "").strip(),
                            actor=actor_name,
                            from_value=_maybe_str(h.get("from")),
                            to_value=_maybe_str(h.get("to")),
                            data=_maybe_str(h.get("data")),
                        )
                    )

                issues.append(
                    LinearIssueWithChanges(
                        identifier=identifier,
                        title=title,
                        url=url,
                        updated_at=updated_at,
                        changes=sorted(changes, key=lambda c: c.created_at),
                    )
                )

            page_info = issues_payload.get("pageInfo")
            if not isinstance(page_info, dict) or not page_info.get("hasNextPage"):
                break
            end_cursor = page_info.get("endCursor")
            after = str(end_cursor) if end_cursor else None
            if not after:
                break

        return sorted(issues, key=lambda i: i.identifier)

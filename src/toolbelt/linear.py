from datetime import datetime
from pathlib import Path
from typing import Any, Optional, TypeVar

import httpx
import typer
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from toolbelt.env_var import get_env_var_or_exit
from toolbelt.logger import logger


class LinearGraphQLError(RuntimeError):
    def __init__(self, message: str, *, errors: list[Any] | None = None):
        super().__init__(message)
        self.errors = errors or []


class LinearTeam(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    key: str
    name: str


class LinearViewer(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str


class LinearIssue(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    identifier: str
    title: str
    description: str = ""
    url: str
    team_id: str
    state_id: str
    state_type: str


class LinearIssueChange(BaseModel):
    model_config = ConfigDict(frozen=True)

    created_at: datetime
    type: str
    actor: Optional[str] = None
    from_value: Optional[str] = None
    to_value: Optional[str] = None
    data: Optional[str] = None


class LinearIssueWithChanges(BaseModel):
    model_config = ConfigDict(frozen=True)

    identifier: str
    title: str
    url: str
    updated_at: datetime
    changes: list[LinearIssueChange]


class _GraphQLErrorItem(BaseModel):
    message: str


class _GraphQLResponse(BaseModel):
    data: Any | None = None
    errors: list[_GraphQLErrorItem] = Field(default_factory=list)


class _ViewerId(BaseModel):
    id: str


class _Viewer(BaseModel):
    id: str
    name: str


class _TeamRef(BaseModel):
    id: str


class _StateRef(BaseModel):
    id: str
    type: str


class _IssueNode(BaseModel):
    id: str
    identifier: str
    title: str
    description: str = ""
    url: str
    team: _TeamRef
    state: _StateRef


class _PageInfo(BaseModel):
    has_next_page: bool = Field(alias="hasNextPage")
    end_cursor: Optional[str] = Field(default=None, alias="endCursor")


class _IssuesConnection(BaseModel):
    nodes: list[_IssueNode]
    page_info: _PageInfo = Field(alias="pageInfo")


class _TeamsConnection(BaseModel):
    nodes: list[LinearTeam]


class _ViewerWithTeams(BaseModel):
    teams: _TeamsConnection


class _Actor(BaseModel):
    name: str


class _HistoryNode(BaseModel):
    created_at: datetime = Field(alias="createdAt")
    type: str
    from_value: Optional[str] = Field(default=None, alias="from")
    to_value: Optional[str] = Field(default=None, alias="to")
    data: Optional[str] = None
    actor: Optional[_Actor] = None

    @field_validator("created_at", mode="before")
    @classmethod
    def _fix_zulu_created_at(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.replace("Z", "+00:00")
        return v


class _HistoryConnection(BaseModel):
    nodes: list[_HistoryNode]


class _InProgressIssueNode(BaseModel):
    identifier: str
    title: str
    url: str
    updated_at: datetime = Field(alias="updatedAt")
    history: Optional[_HistoryConnection] = None

    @field_validator("updated_at", mode="before")
    @classmethod
    def _fix_zulu_updated_at(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.replace("Z", "+00:00")
        return v


class _InProgressConnection(BaseModel):
    nodes: list[_InProgressIssueNode]
    page_info: _PageInfo = Field(alias="pageInfo")


class _IssueCreatePayload(BaseModel):
    issue: _IssueNode


class _IssueUpdatePayload(BaseModel):
    success: bool


TModel = TypeVar("TModel", bound=BaseModel)


class LinearClient:
    def __init__(self, *, api_key: str) -> None:
        self._api_key = api_key
        self._client: httpx.AsyncClient | None = None
        self._query_cache: dict[str, str] = {}

    @classmethod
    def from_env(cls) -> "LinearClient":
        api_key = get_env_var_or_exit("LINEAR_API_KEY").strip()
        if not api_key:
            logger.error("LINEAR_API_KEY is set but empty.")
            raise typer.Exit(1)
        return cls(api_key=api_key)

    async def __aenter__(self) -> "LinearClient":
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        self._client = httpx.AsyncClient(headers=headers, timeout=20.0)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._client is not None:
            await self._client.aclose()
        self._client = None

    def _load_query(self, name: str) -> str:
        cached = self._query_cache.get(name)
        if cached is not None:
            return cached
        base = Path(__file__).with_name("linear_graphql")
        path = base / f"{name}.graphql"
        query = path.read_text(encoding="utf-8")
        self._query_cache[name] = query
        return query

    async def _request(
        self,
        *,
        query_name: str,
        data_model: type[TModel],
        variables: dict[str, Any] | None = None,
    ) -> TModel:
        if self._client is None:
            raise RuntimeError("LinearClient must be used as an async context manager")
        query = self._load_query(query_name)
        resp = await self._client.post(
            "https://api.linear.app/graphql",
            json={"query": query, "variables": variables or {}},
        )
        resp.raise_for_status()

        try:
            payload = _GraphQLResponse.model_validate(resp.json())
        except ValidationError as err:
            raise LinearGraphQLError(
                f"Invalid Linear GraphQL response for '{query_name}'",
                errors=list(err.errors()),
            ) from err

        if payload.errors:
            messages = ", ".join(e.message for e in payload.errors)
            raise LinearGraphQLError(
                messages,
                errors=[e.model_dump() for e in payload.errors],
            )

        if payload.data is None:
            raise LinearGraphQLError("Linear GraphQL response missing data")

        try:
            return data_model.model_validate(payload.data)
        except ValidationError as err:
            raise LinearGraphQLError(
                f"Invalid Linear GraphQL response for '{query_name}'",
                errors=list(err.errors()),
            ) from err

    async def _get_viewer_id(self) -> str:
        class _Data(BaseModel):
            viewer: _ViewerId

        data = await self._request(query_name="viewer_id", data_model=_Data)
        return data.viewer.id

    async def get_viewer(self) -> LinearViewer:
        class _Data(BaseModel):
            viewer: _Viewer

        data = await self._request(query_name="viewer", data_model=_Data)
        return LinearViewer(id=data.viewer.id, name=data.viewer.name)

    async def get_viewer_teams(self) -> list[LinearTeam]:
        class _Data(BaseModel):
            viewer: _ViewerWithTeams

        data = await self._request(query_name="viewer_teams", data_model=_Data)
        return data.viewer.teams.nodes

    async def get_team_started_state_id(self, *, team_id: str) -> str:
        class _StateNode(BaseModel):
            id: str
            type: str

        class _StatesConnection(BaseModel):
            nodes: list[_StateNode]

        class _Team(BaseModel):
            states: _StatesConnection

        class _Data(BaseModel):
            team: _Team

        data = await self._request(
            query_name="team_states",
            data_model=_Data,
            variables={"teamId": team_id},
        )
        for state in data.team.states.nodes:
            if state.type == "started":
                return state.id
        raise LinearGraphQLError("No 'started' workflow state found for team")

    async def get_backlog_issues_assigned_to_viewer(
        self,
        *,
        max_issues: int = 50,
    ) -> list[LinearIssue]:
        viewer_id = await self._get_viewer_id()
        issues: list[LinearIssue] = []
        after: str | None = None
        query_name = "issues_backlog_in"
        use_state_types_var = True

        class _Data(BaseModel):
            issues: _IssuesConnection

        while len(issues) < max_issues:
            first = min(50, max_issues - len(issues))
            variables: dict[str, Any] = {
                "assigneeId": viewer_id,
                "first": first,
                "after": after,
            }
            if use_state_types_var:
                variables["stateTypes"] = ["backlog", "unstarted"]

            try:
                data = await self._request(
                    query_name=query_name,
                    data_model=_Data,
                    variables=variables,
                )
            except LinearGraphQLError:
                if use_state_types_var:
                    use_state_types_var = False
                    query_name = "issues_backlog_or"
                    continue
                raise

            nodes = data.issues.nodes
            if not nodes:
                break

            issues.extend(
                [
                    LinearIssue(
                        id=n.id,
                        identifier=n.identifier,
                        title=n.title,
                        description=n.description,
                        url=n.url,
                        team_id=n.team.id,
                        state_id=n.state.id,
                        state_type=n.state.type,
                    )
                    for n in nodes
                ]
            )

            if not data.issues.page_info.has_next_page:
                break
            after = data.issues.page_info.end_cursor
            if not after:
                break

        return sorted(issues, key=lambda i: i.identifier)

    async def get_issue_by_identifier(self, *, identifier: str) -> LinearIssue:
        class _Data(BaseModel):
            issues: _IssuesConnection

        data = await self._request(
            query_name="issue_by_identifier",
            data_model=_Data,
            variables={"identifier": identifier},
        )
        if not data.issues.nodes:
            raise LinearGraphQLError(f"Issue not found: {identifier}")
        n = data.issues.nodes[0]
        return LinearIssue(
            id=n.id,
            identifier=n.identifier,
            title=n.title,
            description=n.description,
            url=n.url,
            team_id=n.team.id,
            state_id=n.state.id,
            state_type=n.state.type,
        )

    async def update_issue_description(
        self, *, issue_id: str, description: str
    ) -> None:
        class _Data(BaseModel):
            issueUpdate: _IssueUpdatePayload

        await self._request(
            query_name="issue_update_description",
            data_model=_Data,
            variables={"issueId": issue_id, "description": description},
        )

    async def move_issue_to_started(
        self, *, issue_id: str, started_state_id: str
    ) -> None:
        class _Data(BaseModel):
            issueUpdate: _IssueUpdatePayload

        await self._request(
            query_name="issue_move_to_started",
            data_model=_Data,
            variables={"issueId": issue_id, "stateId": started_state_id},
        )

    async def create_issue(
        self,
        *,
        team_id: str,
        title: str,
        description: str,
        assignee_id: str,
        started_state_id: str,
    ) -> LinearIssue:
        class _Data(BaseModel):
            issueCreate: _IssueCreatePayload

        data = await self._request(
            query_name="issue_create",
            data_model=_Data,
            variables={
                "teamId": team_id,
                "title": title,
                "description": description,
                "assigneeId": assignee_id,
                "stateId": started_state_id,
            },
        )
        n = data.issueCreate.issue
        return LinearIssue(
            id=n.id,
            identifier=n.identifier,
            title=n.title,
            description=n.description,
            url=n.url,
            team_id=n.team.id,
            state_id=n.state.id,
            state_type=n.state.type,
        )

    async def get_in_progress_issues_with_changes_since(
        self,
        *,
        since: datetime,
        max_issues: int = 50,
        history_first: int = 25,
    ) -> list[LinearIssueWithChanges]:
        viewer_id = await self._get_viewer_id()

        issues: list[LinearIssueWithChanges] = []
        after: str | None = None
        query_name = "issues_in_progress_with_history"
        history_enabled = True

        class _Data(BaseModel):
            issues: _InProgressConnection

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
                data = await self._request(
                    query_name=query_name,
                    data_model=_Data,
                    variables=variables,
                )
            except LinearGraphQLError:
                if history_enabled:
                    history_enabled = False
                    query_name = "issues_in_progress_no_history"
                    continue
                raise

            nodes = data.issues.nodes
            if not nodes:
                break

            for n in nodes:
                history_nodes = n.history.nodes if (n.history is not None) else []
                changes = [
                    LinearIssueChange(
                        created_at=h.created_at,
                        type=h.type,
                        actor=h.actor.name if h.actor is not None else None,
                        from_value=h.from_value,
                        to_value=h.to_value,
                        data=h.data,
                    )
                    for h in history_nodes
                    if h.created_at >= since
                ]
                issues.append(
                    LinearIssueWithChanges(
                        identifier=n.identifier,
                        title=n.title,
                        url=n.url,
                        updated_at=n.updated_at,
                        changes=sorted(changes, key=lambda c: c.created_at),
                    )
                )

            if not data.issues.page_info.has_next_page:
                break
            after = data.issues.page_info.end_cursor
            if not after:
                break

        return sorted(issues, key=lambda i: i.identifier)

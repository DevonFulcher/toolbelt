import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Any

import httpx
from pydantic import BaseModel

from toolbelt.logger import logger


@dataclass
class RateLimitState:
    limit: int
    remaining: int
    reset_epoch: int


class ResourceType(str, Enum):
    CORE = "core"
    SEARCH = "search"
    CODE_SEARCH = "code_search"


class GithubClient:
    def __init__(self, token: str) -> None:
        self._client = httpx.AsyncClient(
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
            },
            timeout=10.0,
        )
        self._resources: dict[ResourceType, RateLimitState] = {}
        self._rate_limit_buffer = 10.0
        self._max_retries = 3
        self._base_backoff = 0.5
        self._retry_statuses = {429, 500, 502, 503, 504}
        self._locks: dict[ResourceType, asyncio.Lock] = {}

    async def _get(self, url: str, **kwargs: Any) -> httpx.Response:
        last_response: httpx.Response | None = None
        resource = self._resource_for_url(url)
        for attempt in range(self._max_retries):
            async with self._get_lock(resource):
                await self._maybe_sleep(resource)
                response = await self._client.get(url, **kwargs)
                self._update_from_response(response)
            last_response = response
            if (
                response.status_code in self._retry_statuses
                and attempt < self._max_retries - 1
            ):
                delay = self._retry_after_delay(response)
                if delay is None:
                    delay = self._base_backoff * (2**attempt)
                logger.info(
                    "Retrying request to %s in %.1fs (status=%s)",
                    url,
                    delay,
                    response.status_code,
                )
                await asyncio.sleep(delay)
                continue
            return response
        if last_response is None:
            raise httpx.HTTPError("Request failed without response")
        raise httpx.HTTPStatusError(
            "Retries exceeded",
            request=last_response.request,
            response=last_response,
        )

    async def paged_get[TModel: BaseModel](
        self,
        url: str,
        model_cls: type[TModel],
    ) -> list[TModel]:
        async def fetch_page(page: int) -> list[TModel]:
            response = await self._get(url, params={"per_page": 100, "page": page})
            response.raise_for_status()
            data = response.json()
            return [model_cls.model_validate(item) for item in data]

        return await self._paginate(fetch_page)

    async def get_model[TModel: BaseModel](
        self,
        url: str,
        model_cls: type[TModel],
    ) -> TModel:
        response = await self._get(url)
        response.raise_for_status()
        return model_cls.model_validate(response.json())

    async def _paginate[TModel: BaseModel](
        self,
        fetch_page: Callable[[int], Awaitable[list[TModel]]],
    ) -> list[TModel]:
        items: list[TModel] = []
        page = 1
        while True:
            page_items = await fetch_page(page)
            if not page_items:
                break
            items.extend(page_items)
            page += 1
        return items

    def _update_from_response(self, response: httpx.Response) -> None:
        headers = response.headers
        resource = headers.get("X-RateLimit-Resource")
        limit = headers.get("X-RateLimit-Limit")
        remaining = headers.get("X-RateLimit-Remaining")
        reset = headers.get("X-RateLimit-Reset")

        if not (resource and limit and remaining and reset):
            return
        resource_type = self._parse_resource_header(resource)
        if not resource_type:
            return

        try:
            self._resources[resource_type] = RateLimitState(
                limit=int(limit),
                remaining=int(remaining),
                reset_epoch=int(reset),
            )
        except ValueError:
            return

    def _get_lock(self, resource: ResourceType) -> asyncio.Lock:
        if resource not in self._locks:
            self._locks[resource] = asyncio.Lock()
        return self._locks[resource]

    def _resource_for_url(self, url: str) -> ResourceType:
        if "/search/code" in url:
            return ResourceType.CODE_SEARCH
        if "/search/" in url:
            return ResourceType.SEARCH
        return ResourceType.CORE

    @staticmethod
    def _parse_resource_header(resource: str) -> ResourceType | None:
        try:
            return ResourceType(resource)
        except ValueError:
            return None

    @staticmethod
    def _retry_after_delay(response: httpx.Response) -> float | None:
        retry_after = response.headers.get("Retry-After")
        if not retry_after:
            return None
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            return None

    async def _maybe_sleep(self, resource: ResourceType) -> None:
        now = int(time.time())
        info = self._resources.get(resource)
        if not info:
            return
        if info.remaining <= self._rate_limit_buffer:
            reset_in = max(0, info.reset_epoch - now)
            logger.info(
                "Rate limit exhausted for %s; sleeping %ss",
                resource,
                reset_in,
            )
            if reset_in:
                await asyncio.sleep(reset_in)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "GithubClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()


@asynccontextmanager
async def build_async_github_client(
    token: str,
) -> AsyncIterator[GithubClient]:
    async with GithubClient(token) as client:
        yield client

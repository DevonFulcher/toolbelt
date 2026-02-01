import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel

from toolbelt.logger import logger


@dataclass
class RateLimitState:
    limit: int
    remaining: int
    reset_epoch: int


class GithubClient:
    def __init__(self, token: str) -> None:
        self._client = httpx.AsyncClient(
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
            },
            timeout=10.0,
        )
        self._resources: dict[str, RateLimitState] = {}
        self._safety_fraction = 0.1
        self._min_buffer = 1
        self._lock = asyncio.Lock()

    async def _get(self, url: str, **kwargs: Any) -> httpx.Response:
        async with self._lock:
            await self._maybe_sleep()
            response = await self._client.get(url, **kwargs)
            self._update_from_response(response)
            return response

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

        try:
            self._resources[resource] = RateLimitState(
                limit=int(limit),
                remaining=int(remaining),
                reset_epoch=int(reset),
            )
        except ValueError:
            return

    async def _maybe_sleep(self) -> None:
        now = int(time.time())
        for resource, info in self._resources.items():
            buffer = max(self._min_buffer, int(info.limit * self._safety_fraction))
            if info.remaining <= buffer:
                reset_in = max(0, info.reset_epoch - now)
                logger.debug(
                    "Rate limit low for %s; sleeping %ss", resource, reset_in + 1
                )
                await asyncio.sleep(reset_in + 1)
                break

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

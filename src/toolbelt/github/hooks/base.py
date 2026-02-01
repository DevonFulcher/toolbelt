from abc import ABC, abstractmethod
from dataclasses import dataclass

from toolbelt.github.api import IssueComment, Review, ReviewComment


@dataclass
class ReviewWithComments:
    review: Review
    comments: list[ReviewComment]


@dataclass(frozen=True)
class PrRef:
    repo: str
    number: int

    @property
    def pr_url(self) -> str:
        return f"https://github.com/{self.repo}/pull/{self.number}"


class AbstractPrMonitorHooks(ABC):
    @abstractmethod
    async def on_merge_conflict(self, pr: PrRef) -> None:
        raise NotImplementedError

    @abstractmethod
    async def on_new_review(
        self,
        pr: PrRef,
        reviews: list[ReviewWithComments],
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def on_new_issue_comment(
        self,
        pr: PrRef,
        comments: list[IssueComment],
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    async def on_ci_status_change(
        self,
        pr: PrRef,
        has_failure: bool,
    ) -> None:
        raise NotImplementedError

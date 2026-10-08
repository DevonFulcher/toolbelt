"""Unit tests for `is_published`'s pure parsing and its not-configured guard."""

import asyncio

from toolbelt.git.stack.published import _has_results, is_published


def test_has_results_true_when_messages_found():
    payload = {"ok": True, "messages": {"total": 2}}
    assert _has_results(payload) is True


def test_has_results_false_when_no_messages():
    payload = {"ok": True, "messages": {"total": 0}}
    assert _has_results(payload) is False


def test_has_results_false_when_messages_key_missing():
    assert _has_results({"ok": True}) is False


def test_is_published_is_none_when_not_configured(monkeypatch):
    monkeypatch.delenv("SLACK_USER_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_PUBLISHED_CHANNEL", raising=False)

    result = asyncio.run(is_published("https://github.com/acme/widgets/pull/1"))

    assert result is None


def test_is_published_is_none_when_only_token_set(monkeypatch):
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-fake")
    monkeypatch.delenv("SLACK_PUBLISHED_CHANNEL", raising=False)

    result = asyncio.run(is_published("https://github.com/acme/widgets/pull/1"))

    assert result is None


def test_is_published_is_none_when_only_channel_set(monkeypatch):
    monkeypatch.delenv("SLACK_USER_TOKEN", raising=False)
    monkeypatch.setenv("SLACK_PUBLISHED_CHANNEL", "some-channel")

    result = asyncio.run(is_published("https://github.com/acme/widgets/pull/1"))

    assert result is None

"""The dollar cost of one model response, from the ``genai-prices`` table.

``genai-prices`` reproduces what Claude Code itself reports for a call
(including the 5-minute / 1-hour cache-write split), and ships the table with
the package. Codex usage uses OpenAI's input/output and cached-input counts.
"""

from collections.abc import Mapping
from typing import Any

lazy from genai_prices import calc_price, extract_usage

from toolbelt.git.stack.store import TokenCounts
from toolbelt.token_spend.transcripts import tokens_of


def price_usage(model: str, usage: Mapping[str, Any]) -> float | None:
    """The cost in US dollars of ``usage`` (an Anthropic response's ``usage``
    object) on ``model``, or None when the price table has no such model.

    The usage is rebuilt from its parsed token counts first: a response
    written on several transcript rows can have a partial row whose cache
    totals disagree with its own breakdown, which the table rejects."""
    tokens = tokens_of(dict(usage))
    normalized: dict[str, Any] = {
        "input_tokens": tokens.input,
        "output_tokens": tokens.output,
        "cache_read_input_tokens": tokens.cache_read,
        "cache_creation_input_tokens": tokens.cache_write_5m + tokens.cache_write_1h,
        "cache_creation": {
            "ephemeral_5m_input_tokens": tokens.cache_write_5m,
            "ephemeral_1h_input_tokens": tokens.cache_write_1h,
        },
    }
    if usage.get("server_tool_use"):
        normalized["server_tool_use"] = usage["server_tool_use"]
    extracted = extract_usage(
        {"usage": normalized}, provider_id="anthropic", api_flavor="default"
    )
    try:
        price = calc_price(extracted.usage, model, provider_id="anthropic")
    except LookupError:
        return None
    return float(price.total_price)


def price_codex_usage(model: str, tokens: TokenCounts) -> float | None:
    """Price Codex-reported OpenAI token counts at public list prices."""
    normalized: dict[str, Any] = {
        "input_tokens": tokens.input + tokens.cache_read,
        "output_tokens": tokens.output,
        "input_tokens_details": {"cached_tokens": tokens.cache_read},
    }
    extracted = extract_usage(
        {"usage": normalized}, provider_id="openai", api_flavor="responses"
    )
    try:
        price = calc_price(extracted.usage, model, provider_id="openai")
    except LookupError:
        return None
    return float(price.total_price)

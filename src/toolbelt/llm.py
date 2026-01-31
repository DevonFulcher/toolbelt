import os
import re

import typer
from openai import OpenAI

from toolbelt.logger import logger


def generate_short_title_from_plan(*, plan: str) -> str:
    """
    Generate a short ticket title from a plan using OpenAI Responses API.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        logger.error("OPENAI_API_KEY environment variable is not set.")
        raise typer.Exit(1)

    client = OpenAI(api_key=api_key)
    prompt = (
        "Write a short, specific engineering task title for this plan.\n"
        "Return ONLY the title text (no quotes, no prefixes, no trailing period).\n\n"
        f"{plan.strip()}\n"
    )

    title = client.responses.create(
        model="gpt-4o-mini",
        input=prompt,
    ).output_text

    title = _sanitize_title(title)
    if not title:
        logger.error("Failed to generate a ticket title from the plan.")
        raise typer.Exit(1)
    return title


def _sanitize_title(raw: str) -> str:
    title = raw.strip().strip('"').strip("'").strip()
    title = re.sub(r"\s+", " ", title)
    title = title.removeprefix("- ").strip()
    title = title.removesuffix(".").strip()
    # Keep titles reasonably short.
    if len(title) > 120:
        logger.error("Title is too long. Truncating to 120 characters.")
        title = title[:120].rstrip()
    return title

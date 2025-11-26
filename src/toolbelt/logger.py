"""Centralized logging configuration for toolbelt."""

import logging
import sys
from typing import Optional


class WrenchFormatter(logging.Formatter):
    """Custom formatter that adds a wrench emoji to every log line."""

    def format(self, record: logging.LogRecord) -> str:
        """Format the log record with a wrench emoji prefix."""
        # Format the message using the parent formatter
        formatted_message = super().format(record)
        # Add wrench emoji at the start
        return f"🔧 {formatted_message}"


def setup_logging(level: int = logging.INFO) -> None:
    """Set up centralized logging configuration."""
    # Get the root logger
    logger = logging.getLogger()
    logger.setLevel(level)

    # Remove existing handlers to avoid duplicates
    logger.handlers.clear()

    # Create console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)

    # Create formatter with wrench emoji
    formatter = WrenchFormatter(
        fmt="%(message)s",  # Simple format, just the message (emoji added in format method)
        datefmt=None,
    )
    console_handler.setFormatter(formatter)

    # Add handler to logger
    logger.addHandler(console_handler)


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """Get a logger instance, setting up logging if not already configured."""
    # Set up logging if not already configured
    if not logging.getLogger().handlers:
        setup_logging()

    if name:
        return logging.getLogger(name)
    return logging.getLogger()


# Set up logging on module import
setup_logging()

# Export a default logger for convenience
logger = get_logger("toolbelt")

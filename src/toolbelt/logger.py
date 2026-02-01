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
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Remove existing handlers to avoid duplicates
    root_logger.handlers.clear()

    # Create console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)

    # Create formatter with wrench emoji
    # Simple format, just the message (emoji added in format method)
    formatter = WrenchFormatter(
        fmt="%(message)s",
        datefmt=None,
    )
    console_handler.setFormatter(formatter)

    # Add handler to logger
    root_logger.addHandler(console_handler)


def setup_app_only_logging(level: int = logging.INFO) -> None:
    """Set up logging to emit only toolbelt application logs."""
    root_logger = logging.getLogger()

    # Remove existing handlers to avoid duplicates
    root_logger.handlers.clear()
    root_logger.setLevel(logging.WARNING)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(
        WrenchFormatter(
            fmt="%(message)s",
            datefmt=None,
        )
    )

    app_logger = logging.getLogger("toolbelt")
    app_logger.setLevel(level)
    app_logger.handlers.clear()
    app_logger.addHandler(console_handler)
    app_logger.propagate = False


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

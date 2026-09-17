import logging
import os
from logging.handlers import RotatingFileHandler

import config


def setup_logging(log_dir="logs"):
    """Configure logging for the application."""

    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    logger = logging.getLogger("octobot")
    level = logging.DEBUG if config.DEBUG else logging.INFO
    logger.setLevel(level)
    logger.propagate = False
    # Prevent duplicate handlers if called multiple times
    if logger.handlers:
        for handler in logger.handlers:
            handler.setLevel(level)
        return logger

    # Create formatters
    detailed_formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(module)s.%(funcName)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    simple_formatter = logging.Formatter(
        "%(asctime)s - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    # File handler with rotation
    file_handler = RotatingFileHandler(
        filename=os.path.join(log_dir, "octobot.log"),
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(detailed_formatter)

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(simple_formatter)

    # Add handlers
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger

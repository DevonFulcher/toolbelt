import os
import sys
from pathlib import Path

from toolbelt.logger import logger


def get_env_var_or_exit(env_var_name: str) -> str:
    env_var = os.getenv(env_var_name)
    if env_var is None:
        logger.error(f"{env_var_name} environment variable is not set.")
        sys.exit(1)
    return env_var


def get_git_projects_workdir() -> Path:
    return Path(get_env_var_or_exit("GIT_PROJECTS_WORKDIR"))

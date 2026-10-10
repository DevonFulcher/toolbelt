"""Whether toolbelt is running on a personal laptop rather than a work machine.

``IS_PERSONAL=true`` is exported by the user's dotfiles secrets, which only
interactive shells source; non-interactive shells (e.g. agent shells) do not
see it and are treated as non-personal.
"""

import os
from collections.abc import Mapping

IS_PERSONAL_ENV_VAR = "IS_PERSONAL"


def is_personal(env: Mapping[str, str] = os.environ) -> bool:
    return env.get(IS_PERSONAL_ENV_VAR) == "true"

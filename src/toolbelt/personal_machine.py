"""Whether toolbelt is running on a personal laptop rather than a work machine.

``IS_PERSONAL=true`` is exported from the user's dotfiles via ``.zshenv``
(``secrets/machine.sh``), so non-interactive shells such as agent shells see
it too. Where it is unset the machine is treated as non-personal.
"""

import os
from collections.abc import Mapping

IS_PERSONAL_ENV_VAR = "IS_PERSONAL"


def is_personal(env: Mapping[str, str] = os.environ) -> bool:
    return env.get(IS_PERSONAL_ENV_VAR) == "true"

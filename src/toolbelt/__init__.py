import sys

from toolbelt import invocation_log
from toolbelt.cli import app


def main() -> None:
    log = invocation_log.start()
    invocation_log.run_logged(app, log, sys.argv)


if __name__ == "__main__":
    main()

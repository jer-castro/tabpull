"""Entry point: answer a bare version probe before the heavy imports load."""

import sys
from importlib.metadata import version as installed_version

VERSION_FLAGS = ('-v', '-V', '--version')


def version() -> str:
    return installed_version('tabpull')


def main() -> None:
    if any(sys.argv[1:] == [flag] for flag in VERSION_FLAGS):
        print(version())
        return
    # Deferred so `tabpull --version` skips loading Playwright and the REST client.
    from tabpull.app import cli  # noqa: PLC0415

    cli()

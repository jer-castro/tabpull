"""Terminal look: Rich output and questionary pickers when a person is at a TTY.

Piped or captured output keeps the plain TOON and lines that agents and scripts read.
"""

import sys
from typing import Any

import questionary
from rich.console import Console

console = Console()

STYLE = questionary.Style(
    [
        ('qmark', 'fg:#8ab4ff bold'),
        ('pointer', 'fg:#8ab4ff bold'),
        ('highlighted', 'fg:#8ab4ff bold'),
        ('selected', 'fg:#d1dedc'),
        ('answer', 'fg:#d1dedc bold'),
    ]
)


def rich_output() -> bool:
    """Panels, tables, and progress bars instead of plain lines."""
    return sys.stdout.isatty()


def interactive() -> bool:
    """A person can answer pickers."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def ask(question: questionary.Question) -> Any:  # noqa: ANN401 - questionary answers are untyped
    """Run a picker; Ctrl-C raises KeyboardInterrupt for the CLI's exit 130."""
    return question.unsafe_ask()
